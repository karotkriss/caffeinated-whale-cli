"""``cwcli axi`` surface: TOON serializer, verb exit-mapping, content-first home.

Covers task 4.5. The core is stubbed (so these exercise the axi rendering / exit
mapping in isolation): TOON on stdout for success, no progress text on stdout,
typed-error rendering + exit codes, needs-choice -> flag-naming usage error exit
2, and the ls-DTO home populated + empty. Plus the shared TOON encoder's shape.
"""

import importlib
import re
import sys

import click
import pytest
from typer import core as _typer_core
from typer.testing import CliRunner

from caffeinated_whale_cli.commands import axi as axi_mod
from caffeinated_whale_cli.core.backup import BackupOutcome
from caffeinated_whale_cli.core.envelope import Choice, Message, Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind
from caffeinated_whale_cli.core.list import InstanceDTO
from caffeinated_whale_cli.core.where import WhereMatch, WhereResult
from caffeinated_whale_cli.utils import toon

runner = CliRunner()


def _outcome():
    return BackupOutcome(
        site="s.localhost",
        bench_path="/workspace/frappe-bench",
        artifact_path="/workspace/frappe-bench/sites/s.localhost/private/backups/x.sql.gz",
        included_files=False,
    )


def _instances_ok(instances):
    """Patch the ls core producer axi consumes to return a fixed instance list."""
    return lambda **k: Result(status=Status.OK, data=instances)


# ---------------------------------------------------------------------------- axi backup


class TestAxiBackup:
    def test_success_emits_toon_exit_0(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_backup, "backup", lambda *a, **k: Result(status=Status.OK, data=_outcome())
        )
        result = runner.invoke(axi_mod.app, ["backup", "proj", "--site", "s.localhost"])
        assert result.exit_code == 0
        assert "site: s.localhost" in result.stdout
        assert "included_files: false" in result.stdout
        # No progress/status line an agent could misread as data.
        assert "Creating backup" not in result.stdout
        assert "..." not in result.stdout

    def test_warnings_render_in_toon(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_backup,
            "backup",
            lambda *a, **k: Result(
                status=Status.OK,
                data=_outcome(),
                warnings=[Message("default_site.resolved", "Using default site: s.localhost")],
            ),
        )
        result = runner.invoke(axi_mod.app, ["backup", "proj"])
        assert result.exit_code == 0
        assert "warnings[1]:" in result.stdout
        assert "Using default site: s.localhost" in result.stdout

    def test_typed_error_renders_on_stdout_exit_1(self, monkeypatch):
        def _raise(*a, **k):
            raise CwcliError(ErrorKind.NOT_FOUND, "project.not_found", "Project 'proj' not found.")

        monkeypatch.setattr(axi_mod.core_backup, "backup", _raise)
        result = runner.invoke(axi_mod.app, ["backup", "proj"])
        assert result.exit_code == 1
        # The message carries single quotes (TOON-special), so toon.kv quotes it.
        assert result.stdout.startswith("error: \"Project 'proj' not found.\"")
        assert "Traceback" not in result.stdout

    def test_usage_error_exit_2(self, monkeypatch):
        def _raise(*a, **k):
            raise CwcliError(ErrorKind.USAGE, "site.invalid_chars", "Invalid site name 'a;b'.")

        monkeypatch.setattr(axi_mod.core_backup, "backup", _raise)
        result = runner.invoke(axi_mod.app, ["backup", "proj", "--site", "a;b"])
        assert result.exit_code == 2
        assert 'error: "Invalid site name' in result.stdout

    def test_error_hint_rendered(self, monkeypatch):
        """A CwcliError hint is surfaced as a `help:` line in axi output."""

        def _raise(*a, **k):
            raise CwcliError(ErrorKind.NOT_RUNNING, "x", "not running", hint="cwcli start proj")

        monkeypatch.setattr(axi_mod.core_backup, "backup", _raise)
        result = runner.invoke(axi_mod.app, ["backup", "proj"])
        assert result.exit_code == 1
        assert "help: cwcli start proj" in result.stdout

    def test_simple_error_message_stays_unquoted(self, monkeypatch):
        """A special-char-free message renders as a plain `error: <msg>` TOON line."""

        def _raise(*a, **k):
            raise CwcliError(ErrorKind.NOT_RUNNING, "x", "container is not running")

        monkeypatch.setattr(axi_mod.core_backup, "backup", _raise)
        result = runner.invoke(axi_mod.app, ["backup", "proj"])
        assert result.stdout.splitlines()[0] == "error: container is not running"

    def test_colon_bearing_error_message_is_quoted(self, monkeypatch):
        """A message with a stray colon is TOON-quoted so a strict parser can't mis-split it."""

        def _raise(*a, **k):
            raise CwcliError(ErrorKind.PRECONDITION, "x", "connection refused: port 8000")

        monkeypatch.setattr(axi_mod.core_backup, "backup", _raise)
        result = runner.invoke(axi_mod.app, ["backup", "proj"])
        assert result.stdout.splitlines()[0] == 'error: "connection refused: port 8000"'

    def test_needs_choice_select_bench_is_usage_error_exit_2(self, monkeypatch):
        choice = Choice(
            kind="select_bench",
            param="bench",
            prompt="pick",
            options=[{"value": "0", "label": "/w/b0"}, {"value": "1", "label": "/w/b1"}],
        )
        monkeypatch.setattr(
            axi_mod.core_backup,
            "backup",
            lambda *a, **k: Result(status=Status.NEEDS_CHOICE, choice=choice),
        )
        result = runner.invoke(axi_mod.app, ["backup", "proj"])
        assert result.exit_code == 2
        assert "--bench <index|label>" in result.stdout
        # The benches ride in a proper TOON options[N]: block (uniform TOON, not
        # bare indented lines).
        assert "options[2]:" in result.stdout
        assert "[0] /w/b0" in result.stdout
        assert "[1] /w/b1" in result.stdout

    def test_needs_choice_confirm_start_is_usage_error_exit_2(self, monkeypatch):
        choice = Choice(kind="confirm_start", param="auto_start", prompt="not running. Start it?")
        monkeypatch.setattr(
            axi_mod.core_backup,
            "backup",
            lambda *a, **k: Result(status=Status.NEEDS_CHOICE, choice=choice),
        )
        result = runner.invoke(axi_mod.app, ["backup", "proj"])
        assert result.exit_code == 2
        assert "error:" in result.stdout
        # The never-prompts surface must state the problem, not pose a question:
        # no "?" and not the raw interactive prompt.
        first = result.stdout.splitlines()[0]
        assert first.endswith("?") is False
        assert "Start it?" not in result.stdout
        assert "not running" in result.stdout
        assert "cwcli start" in result.stdout  # the actionable remedy on help:


# ------------------------------------------------------------------------------ axi home


def assert_is_one_toon_document(stdout: str) -> None:
    """Every line is TOON: key lines, counted table/block rows, nested dicts,
    and ``- ``-marked record items - nothing else.

    The home is the one verb that emits guidance rather than a DTO, so it is the
    one place a stray prose line could reach stdout and corrupt the one-TOON-
    document contract. This walks the shape recursively instead of trusting it:
    a top-level line must be a key line, every indented line must be accounted
    for by an enclosing header (a ``name[N]{fields}:`` table expects exactly N
    rows; a ``name[N]:`` block expects exactly N raw rows or ``- `` items, each
    item's fields nesting below it; a bare ``name:`` opens a nested dict), and
    anything else fails.
    """
    header = re.compile(r"^[a-zA-Z_][\w-]*(\[(\d+)\](\{[^}]*\})?)?:( .+)?$")
    lines = [line for line in stdout.splitlines() if line]

    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def consume_children(i: int, indent: int, m: re.Match, text: str) -> int:
        count, fields, rest = m.group(2), m.group(3), m.group(4)
        if rest is not None:
            return i  # `key: value` / inline scalar list - a leaf
        if count is None:
            # bare `key:` - a nested dict; its key lines sit two spaces deeper
            while i < len(lines) and indent_of(lines[i]) >= indent + 2:
                i = parse_keyline(i, indent + 2)
            return i
        n = int(count)
        if fields is not None:
            # tabular block: exactly n rows, scalar cells only
            for _ in range(n):
                assert (
                    i < len(lines) and indent_of(lines[i]) == indent + 2
                ), f"row(s) missing under {text!r}"
                i += 1
        else:
            # counted block: n children, each a `- ` record item or a raw row
            for _ in range(n):
                assert (
                    i < len(lines) and indent_of(lines[i]) == indent + 2
                ), f"item(s) missing under {text!r}"
                if lines[i][indent + 2 :].startswith("- "):
                    i = parse_item(i, indent + 2)
                else:
                    i += 1  # raw block row (help/options text)
        assert (
            i >= len(lines) or indent_of(lines[i]) <= indent
        ), f"unaccounted indented line: {lines[i]!r}"
        return i

    def parse_keyline(i: int, indent: int) -> int:
        line = lines[i]
        assert indent_of(line) == indent, f"misindented line: {line!r}"
        text = line[indent:]
        assert not text.startswith("- "), f"item outside a counted block: {line!r}"
        m = header.match(text)
        assert m, f"not a TOON line: {line!r}"
        return consume_children(i + 1, indent, m, text)

    def parse_item(i: int, dash_indent: int) -> int:
        # `- key: ...`: the first field rides the dash line; the item's remaining
        # fields sit at the same column as that first field.
        line = lines[i]
        first = line[dash_indent + 2 :]
        m = header.match(first)
        assert m, f"not a TOON item line: {line!r}"
        i = consume_children(i + 1, dash_indent + 2, m, first)
        while (
            i < len(lines)
            and indent_of(lines[i]) == dash_indent + 2
            and not lines[i][dash_indent + 2 :].startswith("- ")
        ):
            i = parse_keyline(i, dash_indent + 2)
        return i

    i = 0
    while i < len(lines):
        i = parse_keyline(i, 0)


class TestAxiHome:
    def test_home_is_one_toon_document_not_prose(self, monkeypatch):
        """The content-first home teaches, but it does so in TOON, not prose."""
        monkeypatch.setattr(
            axi_mod.core_list,
            "list_instances",
            _instances_ok([InstanceDTO(project_name="p", status="running", ports=["8000"])]),
        )
        result = runner.invoke(axi_mod.app, [])
        assert result.exit_code == 0
        assert_is_one_toon_document(result.stdout)

    def test_home_empty_state_is_also_one_toon_document(self, monkeypatch):
        monkeypatch.setattr(axi_mod.core_list, "list_instances", _instances_ok([]))
        result = runner.invoke(axi_mod.app, [])
        assert_is_one_toon_document(result.stdout)

    def test_home_docker_error_is_also_one_toon_document(self, monkeypatch):
        def _raise(**k):
            raise CwcliError(ErrorKind.DOCKER, "docker.unreachable", "Could not connect: down.")

        monkeypatch.setattr(axi_mod.core_list, "list_instances", _raise)
        result = runner.invoke(axi_mod.app, [])
        assert_is_one_toon_document(result.stdout)

    def test_home_shows_instances(self, monkeypatch):
        """Bare `axi` renders the instance list as a TOON table."""
        monkeypatch.setattr(
            axi_mod.core_list,
            "list_instances",
            _instances_ok(
                [
                    InstanceDTO(project_name="proj-a", status="running", ports=["8000", "8001"]),
                    InstanceDTO(project_name="proj-b", status="exited", ports=[]),
                ]
            ),
        )
        result = runner.invoke(axi_mod.app, [])
        assert result.exit_code == 0
        assert result.stdout.startswith("bin: ")
        assert "description: " in result.stdout
        assert "instances[2]{projectName,status,ports}:" in result.stdout
        assert "proj-a,running,8000 8001" in result.stdout
        assert "proj-b,exited,N/A" in result.stdout
        # The block declares its own count, so this pins the count/entry agreement
        # rather than a fixed number of suggestions.
        assert "help[5]:" in result.stdout
        assert result.stdout.count("  Run `cwcli axi ") == 5
        # The home curates a FEW verbs (it is the per-session hook payload), so
        # this is the only route from it to the rest of the surface.
        assert "Run `cwcli axi --help` to see every verb" in result.stdout

    def test_home_definitive_empty_state(self, monkeypatch):
        monkeypatch.setattr(axi_mod.core_list, "list_instances", _instances_ok([]))
        result = runner.invoke(axi_mod.app, [])
        assert result.exit_code == 0
        assert "instances: 0 Frappe instances found" in result.stdout
        assert "help[" in result.stdout

    def test_home_docker_error_is_structured_stdout(self, monkeypatch):
        def _raise(**k):
            raise CwcliError(ErrorKind.DOCKER, "docker.unreachable", "Could not connect to Docker.")

        monkeypatch.setattr(axi_mod.core_list, "list_instances", _raise)
        result = runner.invoke(axi_mod.app, [])
        assert result.exit_code == 1
        assert result.stdout.startswith("error: Could not connect to Docker.")
        assert "Traceback" not in result.stdout


# -------------------------------------------------------------------------------- axi ls


class TestAxiLs:
    def test_ls_emits_toon_table_exit_0(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_list,
            "list_instances",
            _instances_ok(
                [
                    InstanceDTO(project_name="proj-a", status="running", ports=["8000", "8001"]),
                    InstanceDTO(project_name="proj-b", status="exited", ports=[]),
                ]
            ),
        )
        result = runner.invoke(axi_mod.app, ["ls"])
        assert result.exit_code == 0
        # Focused verb: JUST the instances block, no bin/description/help wrapper.
        assert result.stdout.startswith("instances[2]{projectName,status,ports}:")
        assert "proj-a,running,8000 8001" in result.stdout
        assert "proj-b,exited,N/A" in result.stdout
        assert "bin:" not in result.stdout

    def test_ls_definitive_empty_state(self, monkeypatch):
        monkeypatch.setattr(axi_mod.core_list, "list_instances", _instances_ok([]))
        result = runner.invoke(axi_mod.app, ["ls"])
        assert result.exit_code == 0
        assert result.stdout.strip() == "instances: 0 Frappe instances found"

    def test_ls_never_emits_json(self, monkeypatch):
        # axi is TOON-only; there is no --json/-j flag on the verb.
        monkeypatch.setattr(axi_mod.core_list, "list_instances", _instances_ok([]))
        assert runner.invoke(axi_mod.app, ["ls", "--json"]).exit_code == 2
        assert runner.invoke(axi_mod.app, ["ls", "-j"]).exit_code == 2

    def test_ls_docker_error_exit_1(self, monkeypatch):
        def _raise(**k):
            raise CwcliError(ErrorKind.DOCKER, "docker.unreachable", "Could not connect to Docker.")

        monkeypatch.setattr(axi_mod.core_list, "list_instances", _raise)
        result = runner.invoke(axi_mod.app, ["ls"])
        assert result.exit_code == 1
        assert result.stdout.startswith("error: Could not connect to Docker.")


# ----------------------------------------------------------------------------- axi where


class TestAxiWhere:
    def test_where_emits_toon_matches_exit_0(self, monkeypatch):
        matches = [
            WhereMatch(
                type="app",
                project="proj-a",
                bench="/w/b",
                name="erpnext",
                version="15.0.0",
                branch="version-15",
                site="s.localhost",
                installed=True,
                project_state="present",
            ),
            WhereMatch(
                type="site",
                project="proj-a",
                bench="/w/b",
                name="s.localhost",
                project_state="present",
            ),
        ]
        monkeypatch.setattr(
            axi_mod.core_where,
            "where",
            lambda *a, **k: Result(
                status=Status.OK, data=WhereResult(matches=matches, verified=True)
            ),
        )
        result = runner.invoke(axi_mod.app, ["where", "erp"])
        assert result.exit_code == 0
        assert result.stdout.startswith(
            "matches[2]{type,project,bench,name,version,branch,site,installed,project_state}:"
        )
        assert "app,proj-a,/w/b,erpnext,15.0.0,version-15,s.localhost,true,present" in result.stdout
        assert "site,proj-a,/w/b,s.localhost,null,null,null,false,present" in result.stdout
        assert "verified: true" in result.stdout

    def test_where_never_presents_a_cached_answer_as_verified(self, monkeypatch):
        """The regression pin: an absent or unverified instance is legible to an agent.

        ``where`` served rows for a removed project, and identical rows while the
        Docker daemon was unreachable, with nothing in the structured output
        separating a live-confirmed hit from a remembered one.
        """
        matches = [
            WhereMatch(
                type="app",
                project="gone",
                bench="/w/b",
                name="erpnext",
                installed=True,
                project_state="absent",
            ),
        ]
        monkeypatch.setattr(
            axi_mod.core_where,
            "where",
            lambda *a, **k: Result(
                status=Status.WARNING,
                data=WhereResult(matches=matches, verified=True),
                warnings=[Message("where.stale_projects", "instance no longer exists: gone")],
            ),
        )
        result = runner.invoke(axi_mod.app, ["where", "erp"])
        assert result.exit_code == 0
        # The row itself carries the distinction - an agent reading only the table
        # must not have to infer staleness from a warning it may not parse.
        assert ",absent" in result.stdout
        assert "present" not in result.stdout
        assert "instance no longer exists: gone" in result.stdout

    def test_where_passes_no_verify_through_to_the_core(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            axi_mod.core_where,
            "where",
            lambda *a, **k: seen.update(k)
            or Result(status=Status.OK, data=WhereResult(matches=[])),
        )
        runner.invoke(axi_mod.app, ["where", "erp"])
        assert seen["verify"] is True
        runner.invoke(axi_mod.app, ["where", "erp", "--no-verify"])
        assert seen["verify"] is False

    def test_where_definitive_empty_state(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_where,
            "where",
            lambda *a, **k: Result(status=Status.OK, data=WhereResult(matches=[])),
        )
        result = runner.invoke(axi_mod.app, ["where", "zzz"])
        assert result.exit_code == 0
        # An empty typed collection is still a definitive TOON empty state; the
        # verification flag rides along so "nothing found" is itself qualified.
        assert result.stdout.strip() == "matches[0]:\nverified: false"

    def test_where_usage_error_exit_2(self, monkeypatch):
        def _raise(*a, **k):
            raise CwcliError(
                ErrorKind.USAGE,
                "where.apps_sites_conflict",
                "Cannot use --apps and --sites together.",
            )

        monkeypatch.setattr(axi_mod.core_where, "where", _raise)
        result = runner.invoke(axi_mod.app, ["where", "x", "--apps", "--sites"])
        assert result.exit_code == 2
        assert "error: Cannot use --apps and --sites together." in result.stdout


# ---------------------------------------------------------------------------- toon encoder


class TestToonEncoder:
    def test_flat_object(self):
        """`toon.encode` renders a flat object with true/null scalars."""
        doc = toon.encode({"a": "x", "b": True, "n": None})
        assert doc == "a: x\nb: true\nn: null"

    def test_numeric_string_quoted(self):
        """`toon.encode` quotes a numeric string so "42" is not read as the number 42."""
        # so an agent doesn't read the string "42" as the number 42
        assert toon.encode({"id": "42"}) == 'id: "42"'
        assert toon.encode({"id": 42}) == "id: 42"

    def test_table_shape(self):
        """`toon.table` emits the `name[count]{cols}` header and CSV-style rows."""
        rows = [{"id": "a", "n": 1}, {"id": "b", "n": 2}]
        out = toon.table("rows", rows, ["id", "n"])
        assert out.splitlines()[0] == "rows[2]{id,n}:"
        assert out.splitlines()[1] == "  a,1"

    def test_value_with_comma_is_quoted(self):
        assert toon.encode({"k": "a,b"}) == 'k: "a,b"'

    def test_block_one_item_per_line(self):
        out = toon.block("help", ["do x", "do y"])
        assert out == "help[2]:\n  do x\n  do y"

    def test_help_can_force_quote_structural_strings(self):
        """Help metadata can be strict TOON without changing ordinary data output."""
        assert (
            toon.kv("usage", "cwcli axi [command] [flags]", force_quote=True)
            == 'usage: "cwcli axi [command] [flags]"'
        )
        out = toon.table(
            "flags",
            [{"name": "--help", "description": "Show help."}],
            ["name", "description"],
            force_quote_fields={"name", "description"},
        )
        assert out.splitlines()[1] == '  "--help","Show help."'

    def test_self_check_runs(self):
        """The toon module's internal self-check passes."""
        toon._self_check()  # asserts internally; must not raise


# ------------------------------------------------------------------- parse-error -> TOON


class TestAxiParseErrorsAreToon:
    """Typer/click parse failures (unknown flag / missing arg / missing option) must
    render as `error:`+`help:` TOON on STDOUT and exit 2, never Typer's rich panel on
    STDERR with empty stdout. The behavior is inherited from the shared ToonGroup, so
    one representative verb per class proves the surface-wide fix, plus a mounted-path
    check that it survives being nested under the human `cwcli` command.
    """

    def test_unknown_flag_is_toon_on_stdout_exit_2(self):
        result = runner.invoke(axi_mod.app, ["ls", "--bogus"])
        assert result.exit_code == 2
        assert result.stdout.startswith("error:")
        assert "--bogus" in result.stdout
        assert "help[1]:" in result.stdout
        assert "usage:" in result.stdout
        assert "Traceback" not in result.stdout

    def test_missing_argument_names_valid_flags(self):
        result = runner.invoke(axi_mod.app, ["backup"])
        assert result.exit_code == 2
        assert result.stdout.startswith("error:")
        assert "PROJECT" in result.stdout
        # The usage line names the command's real flags so an agent self-corrects.
        assert "--site/-s" in result.stdout
        assert "--with-files" in result.stdout

    def test_missing_required_option_names_the_option(self):
        result = runner.invoke(axi_mod.app, ["self-update"])
        assert result.exit_code == 2
        assert result.stdout.startswith("error:")
        assert "--check" in result.stdout
        # Typer's noisy empty-envvar suffix is stripped.
        assert "env var" not in result.stdout

    def test_mounted_under_cwcli_still_toon(self):
        """The fix must survive `cwcli axi <verb>` (axi nested under the human app),
        not only the axi app invoked directly."""
        from caffeinated_whale_cli.main import app as root_app

        result = runner.invoke(root_app, ["axi", "ls", "--bogus"])
        assert result.exit_code == 2
        assert result.stdout.startswith("error:")
        assert "usage: " in result.stdout
        # `cwcli axi ls` chain named in the usage line.
        assert "axi ls" in result.stdout

    def test_human_cli_usage_error_is_untouched(self):
        """A non-axi parse error keeps Typer's default rendering (stderr, no TOON),
        so the emitter is strictly additive to the agent surface."""
        from caffeinated_whale_cli.main import app as root_app

        result = runner.invoke(root_app, ["ls", "--bogus"])
        assert result.exit_code == 2
        assert not result.stdout.startswith("error:")

    def test_emit_usage_error_stdout_is_ansi_free(self, capsys):
        """The emitter writes clean, ANSI-free TOON just like the core-error path."""
        error = click.NoSuchOption("--bogus")
        error.ctx = None  # no ctx: still emits the error line, no help block
        axi_mod.emit_usage_error_as_toon(error)
        out = capsys.readouterr().out
        assert out.startswith("error:")
        assert "\x1b[" not in out  # no ANSI escapes

    def test_bare_cwcli_is_usage_error_not_traceback(self):
        """A bare `cwcli` (no subcommand) must print a usage error and exit 2, never
        escape to the top-level rich traceback. See TestVendoredClickIsCaught for the
        root cause (ToonGroup.main missing the click flavor a vendoring Typer raises)."""
        from caffeinated_whale_cli.main import app as root_app

        result = runner.invoke(root_app, [])
        assert result.exit_code == 2
        assert "Missing command" in result.output
        assert "Traceback" not in result.output


class TestVendoredClickIsCaught:
    """Root cause of the captain's bare-`cwcli` traceback: Typer >= ~0.17 VENDORS
    Click as `typer._click`, so a TyperGroup raises that vendored
    ClickException/UsageError/Abort - a DIFFERENT class hierarchy from the `click`
    package `commands/axi.py` imports. The hardcoded `except click.ClickException`
    then missed them and the error escaped to the top-level rich traceback. The fix
    resolves the caught-sets from whichever Click flavor Typer actually uses.

    These pin the fix independently of the installed Typer version (the repo locks
    an older, standalone-Click Typer where the bug does not reproduce end to end).
    """

    def test_caught_sets_track_the_click_flavor_typer_raises(self):
        """The classes ToonGroup.main catches must be the ones Typer's OWN base group
        raises - resolved live from the base class's module, so this holds on both a
        standalone-Click and a Click-vendoring Typer."""
        base_module = sys.modules[_typer_core.TyperGroup.__mro__[1].__module__]
        click_pkg = base_module.__name__.rsplit(".", 1)[0]  # 'click' or 'typer._click'
        exc = importlib.import_module(click_pkg + ".exceptions")

        assert exc.ClickException in axi_mod._CLICK_EXCEPTIONS
        assert exc.UsageError in axi_mod._USAGE_ERRORS
        assert exc.Abort in axi_mod._ABORTS

    def test_toongroup_renders_a_foreign_hierarchy_usage_error(self, monkeypatch, capsys):
        """Portable reproduction: a UsageError from a foreign hierarchy (as a
        vendoring Typer raises) must be caught and rendered as TOON on the axi
        surface, exit 2 - not escape. Before the fix, `except click.ClickException`
        could not match it, so `group.main` raised instead of exiting."""

        class ForeignClickError(Exception):
            exit_code = 1

            def show(self, file=None):  # pragma: no cover - unused on the TOON path
                pass

        class ForeignUsageError(ForeignClickError):
            exit_code = 2

            def __init__(self, message, ctx=None):
                super().__init__(message)
                self._message = message
                self.ctx = ctx

            def format_message(self):
                return self._message

        # The fix's resolution picks these up under a vendoring Typer; simulate that.
        # raising=False so that BEFORE the fix (no such attributes, and the hardcoded
        # `except click.ClickException` ignores them) the foreign error genuinely
        # escapes `group.main` and this test fails on the real bug, not on setup.
        monkeypatch.setattr(
            axi_mod,
            "_CLICK_EXCEPTIONS",
            (click.ClickException, ForeignClickError),
            raising=False,
        )
        monkeypatch.setattr(
            axi_mod, "_USAGE_ERRORS", (click.UsageError, ForeignUsageError), raising=False
        )

        group = axi_mod.AxiToonGroup(name="axi")
        ctx = click.Context(group, info_name="cwcli axi")
        error = ForeignUsageError("Missing command.", ctx=ctx)

        def raise_foreign(self, *args, **kwargs):
            raise error

        monkeypatch.setattr(_typer_core.TyperGroup, "main", raise_foreign)

        with pytest.raises(SystemExit) as exit_info:
            group.main(args=[], prog_name="cwcli axi", standalone_mode=True)

        assert exit_info.value.code == 2
        out = capsys.readouterr().out
        assert out.startswith("error:")
        assert "Missing command." in out


# ------------------------------------------------------------------------- TOON help


def _axi_help_commands():
    """Walk the real mounted ``cwcli axi`` tree, including every nested group."""
    from typer.main import get_command

    from caffeinated_whale_cli.main import app as root_app

    root = get_command(root_app)
    axi = root.commands["axi"]

    def walk(command, path):
        yield path, command
        if isinstance(command, click.Group):
            ctx = click.Context(command)
            for name in command.list_commands(ctx):
                child = command.get_command(ctx, name)
                assert child is not None
                yield from walk(child, [*path, name])

    yield from walk(axi, ["axi"])


class TestAxiHelpIsToon:
    """Every help path is a complete TOON reference, including future commands."""

    def test_every_registered_help_path_is_complete_toon(self):
        """Walk the registry so a new command cannot silently regain Rich help."""
        from caffeinated_whale_cli.main import app as root_app

        visited: list[tuple[str, ...]] = []
        for path, command in _axi_help_commands():
            visited.append(tuple(path))
            result = runner.invoke(root_app, [*path, "--help"], color=True)

            # Positive proof comes first. Empty or content-stripped output must
            # fail before any decoration check could pass vacuously.
            assert result.exit_code == 0, (path, result.stdout, result.stderr)
            assert result.stdout.startswith("usage:"), path
            assert all(part in result.stdout.splitlines()[0] for part in path), path
            assert "description:" in result.stdout, path
            assert "flags[" in result.stdout, path
            assert "examples[" in result.stdout, path

            visible_params = [param for param in command.params if not param.hidden]
            arguments = [param for param in visible_params if isinstance(param, click.Argument)]
            options = [param for param in visible_params if isinstance(param, click.Option)]
            if arguments:
                assert "arguments[" in result.stdout, path
                for argument in arguments:
                    # D3: the table names an argument by its explicit metavar
                    # when one is declared (e.g. `APP` for `app_name`), matching
                    # the usage line and examples rather than the raw param name.
                    display_name = getattr(argument, "metavar", None) or argument.name
                    assert display_name in result.stdout, (path, display_name)
                    assert str(argument.required).lower() in result.stdout, path
                    assert argument.help in result.stdout, (path, argument.help)
            if isinstance(command, click.Group):
                assert "commands[" in result.stdout, path
                assert "{name,description}" in result.stdout, path
                ctx = click.Context(command)
                for name in command.list_commands(ctx):
                    assert name in result.stdout, (path, name)
                    child = command.get_command(ctx, name)
                    assert child is not None
                    # D2: rendered with limit=100 (Click's default 45 truncated
                    # every description, several mid-clause). D6: leaked RST/
                    # markdown markup is stripped from the row too.
                    expected = axi_mod._strip_prose_markup(child.get_short_help_str(limit=100))
                    assert expected in result.stdout, (path, name)
            for option in options:
                assert option.opts[0] in result.stdout, (path, option.opts[0])
                assert str(option.required).lower() in result.stdout, path
                assert option.help in result.stdout, (path, option.help)

            # Negative proof follows the content assertions.
            examples_line = next(
                line for line in result.stdout.splitlines() if line.startswith("examples[")
            )
            assert ": " in examples_line, path
            assert not any(line.startswith("  cwcli ") for line in result.stdout.splitlines()), path
            assert not any("\u2500" <= char <= "\u257f" for char in result.stdout), path
            assert "\x1b[" not in result.stdout, path
            assert result.stderr == "", path
            for line in result.stdout.splitlines():
                assert line, f"blank alignment line in {' '.join(path)}"
                assert line == line.rstrip(), f"trailing alignment space in {line!r}"
                leading = len(line) - len(line.lstrip(" "))
                assert leading in (0, 2), f"non-TOON indentation in {line!r}"
                assert "  " not in line[leading:], f"column padding in {line!r}"

        assert ("axi",) in visited
        assert ("axi", "scale") in visited
        assert ("axi", "apps", "install") in visited

    def test_constraint_aware_examples_are_valid_operations(self):
        """Examples include the runtime-required mode that parser metadata cannot express."""
        from caffeinated_whale_cli.main import app as root_app

        label = runner.invoke(root_app, ["axi", "label", "--help"])
        label_examples = next(
            line for line in label.stdout.splitlines() if line.startswith("examples[")
        )
        assert "--set <text>" in label_examples
        assert "--clear" in label_examples
        assert "label <project>," not in label_examples

        init = runner.invoke(root_app, ["axi", "init", "--help"])
        init_examples = next(
            line for line in init.stdout.splitlines() if line.startswith("examples[")
        )
        assert init_examples.count('CWCLI_ADMIN_PASSWORD=\\"<password>\\"') == 2
        assert "--no-start" in init_examples

    def test_scale_examples_include_the_runtime_required_consent_flag(self):
        """Regression pin for D1: `--yes` is only conditionally required at
        runtime (``core/scale.py``'s ``confirm_scale``), so it can never carry
        the literal REQUIRED token without lying about the idempotent no-op -
        both generated examples used to omit it and exit 2 for the operation
        `scale` exists to perform."""
        from caffeinated_whale_cli.main import app as root_app

        scale = runner.invoke(root_app, ["axi", "scale", "--help"])
        scale_examples = next(
            line for line in scale.stdout.splitlines() if line.startswith("examples[")
        )
        assert scale_examples.count("--yes") == 2

    def test_no_generated_example_omits_its_command_documented_consent_flag(self):
        """General form of D1, not the `scale` instance: this codebase's convention
        is that any flag gating a runtime ``NEEDS_CHOICE`` names itself in its own
        help text with the word "consent" (``rm``, `rm-site`, `scale` all do). A
        flag meeting that convention must appear in every one of its command's
        generated examples - whether `_param_required` catches it via the literal
        REQUIRED token (`rm`, `rm-site`) or a hand-written branch is needed because
        the flag is only conditionally required (`scale`) - so an example is never
        printed that exits 2 for the command's documented purpose. This class has
        been reached twice by two different routes; this pins the class."""
        for path, command in _axi_help_commands():
            if isinstance(command, click.Group):
                continue
            ctx = click.Context(command)
            consent_flags = [
                option.opts[0]
                for option in command.params
                if isinstance(option, click.Option) and "consent" in (option.help or "").lower()
            ]
            if not consent_flags:
                continue
            for example in axi_mod._help_examples(command, ctx):
                for flag in consent_flags:
                    assert flag in example, (path, flag, example)

    def test_explicit_argument_metavars_are_preserved(self):
        """Usage and examples retain a command's explicit public argument shape."""
        from caffeinated_whale_cli.main import app as root_app

        for command in ("checkout", "install"):
            result = runner.invoke(root_app, ["axi", "apps", command, "--help"])
            usage = result.stdout.splitlines()[0]
            examples = next(
                line for line in result.stdout.splitlines() if line.startswith("examples[")
            )
            assert "<APP>" in usage
            assert "<APP>" in examples
            assert "<app-name>" not in usage
            assert "<app-name>" not in examples

    def test_arguments_table_uses_the_declared_metavar(self):
        """Regression pin for D3: the arguments table used to print the raw
        param name (`app_name`) while the usage line and examples used the
        declared metavar (`APP`) - one document carrying two names for one
        parameter. The table must use the same resolution `_argument_placeholder`
        does, so all three surfaces agree."""
        from caffeinated_whale_cli.main import app as root_app

        for command in ("checkout", "install"):
            result = runner.invoke(root_app, ["axi", "apps", command, "--help"])
            arguments_block = next(
                line for line in result.stdout.splitlines() if line.startswith("arguments[")
            )
            row_start = result.stdout.index(arguments_block) + len(arguments_block)
            rows_text = result.stdout[row_start:]
            assert "\n  APP," in rows_text
            assert "app_name" not in rows_text

    def test_group_listing_descriptions_are_not_truncated(self):
        """Regression pin for D2: Click's default `limit=45` cut every one of the
        27 command descriptions in the two group listings, several mid-clause,
        dropping load-bearing words like READ-ONLY and ONE named site. Passing
        `limit=100` must eliminate every truncation-with-ellipsis, and the two
        READ-ONLY markers (lost to Click's separate stop-at-first-sentence
        behavior, not the limit itself) must survive too."""
        from caffeinated_whale_cli.main import app as root_app

        for path in (["axi"], ["axi", "apps"]):
            result = runner.invoke(root_app, [*path, "--help"])
            rows = []
            in_commands = False
            for line in result.stdout.splitlines():
                if line.startswith("commands["):
                    in_commands = True
                    continue
                if in_commands:
                    if not line.startswith("  "):
                        break
                    rows.append(line)
            assert rows, path
            for row in rows:
                assert not row.rstrip('"').endswith("..."), row

        root = runner.invoke(root_app, ["axi", "--help"])
        assert 'config,"Report the effective cwcli configuration' in root.stdout
        assert (
            "READ-ONLY"
            in [line for line in root.stdout.splitlines() if line.strip().startswith("config,")][0]
        )
        assert (
            "READ-ONLY"
            in [
                line for line in root.stdout.splitlines() if line.strip().startswith("self-update,")
            ][0]
        )
        assert (
            "ONE named site"
            in [line for line in root.stdout.splitlines() if line.strip().startswith("run-tests,")][
                0
            ]
        )

    def test_help_only_second_example_is_dropped(self):
        """Regression pin for D4: a command whose only optional flag is --help
        used to emit `cwcli axi X --help` as a filler second example, which
        teaches an agent nothing it does not already know. Such a command must
        emit exactly one example instead."""
        from caffeinated_whale_cli.main import app as root_app

        for command in ("ls", "config", "setup"):
            result = runner.invoke(root_app, ["axi", command, "--help"])
            examples_line = next(
                line for line in result.stdout.splitlines() if line.startswith("examples[")
            )
            assert examples_line.startswith("examples[1]:"), examples_line
            assert "--help" not in examples_line

    def test_rm_second_example_shows_the_non_destructive_flag(self):
        """Regression pin for D5: rm's second example used to spell out
        `--volumes`, the MORE destructive half of `--volumes/--no-volumes`
        (`--volumes` is already the default). The flag worth showing on the
        repo's most destructive verb is the one that preserves data."""
        from caffeinated_whale_cli.main import app as root_app

        result = runner.invoke(root_app, ["axi", "rm", "--help"])
        examples_line = next(
            line for line in result.stdout.splitlines() if line.startswith("examples[")
        )
        assert "--no-volumes" in examples_line
        assert "--yes --volumes" not in examples_line

    def test_notes_have_no_leaked_markdown_markup(self):
        """Regression pin for D6: raw markdown ``` `code` ``` and `**bold**`
        markers used to leak into notes/description text verbatim, since Rich
        stripped them but the TOON renderer read `command.help` directly."""
        for path, command in _axi_help_commands():
            for paragraph in axi_mod._help_paragraphs(command.help):
                assert "`" not in paragraph, (path, paragraph)
                assert "**" not in paragraph, (path, paragraph)

    def test_bullet_lists_stay_separate_note_rows(self):
        """Regression pin for D7: a `- ` bullet list used to collapse into one
        run-on paragraph because `_help_paragraphs` joined every line in a block
        with spaces; each item must survive as its own entry, as Rich rendered
        them as separate bullets. Also pins the false-positive guard: a plain
        prose paragraph that merely word-wraps onto a line starting with "- "
        (migrate's BLAST RADIUS paragraph) must NOT be split."""
        from caffeinated_whale_cli.main import app as root_app

        migrate = runner.invoke(root_app, ["axi", "migrate", "--help"])
        notes = [
            line.strip()
            for line in migrate.stdout.splitlines()
            if line.strip().startswith('"') and "guard against a named threat" not in line
        ]
        # Four distinct safety-posture bullets, each its own note row.
        assert any(note.startswith('"EXACTLY ONE site') for note in notes)
        assert any(note.startswith('"Maintenance mode is enabled') for note in notes)
        assert any(note.startswith('"A site whose migrate lock') for note in notes)
        assert any(note.startswith('"NO --yes and no auto-start') for note in notes)
        # No row runs all four bullets together.
        assert not any(
            "EXACTLY ONE site" in note and "Maintenance mode is enabled" in note for note in notes
        )
        # The wrapped BLAST RADIUS prose (ends a sentence with "no rollback" then
        # wraps onto "- a patch...") stays one paragraph, not a bullet split.
        assert any(
            "no rollback - a patch that fails partway" in note
            for note in migrate.stdout.split("\n")
        )

    def test_human_help_keeps_rich_rendering(self):
        """The sibling human surface remains the existing decorated help."""
        from caffeinated_whale_cli.main import app as root_app

        result = runner.invoke(root_app, ["scale", "--help"], color=True)
        assert result.exit_code == 0
        assert "Usage:" in result.stdout
        assert "Arguments" in result.stdout
        assert "--to" in click.unstyle(result.stdout)
        assert any("\u2500" <= char <= "\u257f" for char in result.stdout)
        assert not result.stdout.startswith("usage:")


class TestNoAxiRunVerb:
    """There is deliberately NO ``axi run``/``axi exec`` verb. ``README.md`` records
    that the ``apps`` group exists specifically to replace "dropping to the raw
    ``cwcli run <project> bench get-app ...`` escape hatch", so giving ``run`` an
    axi verb would re-open the exact escape hatch ``apps`` was built to close
    (`openspec/changes/migrate-label-core/proposal.md:63`). This test keeps the
    absence a decision, not an oversight (the ``axi open`` non-verb precedent).

    ``cwcli axi run-tests`` (shipped 2026-07-20, `add-axi-bench-exec-verbs`) does
    NOT weaken this: the assertions compare EXACT command names, so a verb whose
    name merely starts with "run" is untouched by them. The substantive distinction
    is who AUTHORS the command string - ``axi run`` takes an unbounded one, while
    ``run-tests`` takes a fixed bench subcommand with typed, individually-quoted
    parameters. ``tests/test_axi_bench_ops.py`` asserts none of its parameters is
    variadic or free-form, so that distinction is checked rather than merely
    claimed. Do not read the hyphen as an erosion."""

    def test_axi_registry_has_no_run_or_exec_command(self):
        registered = {c.name for c in axi_mod.app.registered_commands}
        assert "run" not in registered
        assert "exec" not in registered
        # Nor under any axi subapp (e.g. `axi apps ...`).
        for group in axi_mod.app.registered_groups:
            sub = {c.name for c in group.typer_instance.registered_commands}
            assert "run" not in sub
            assert "exec" not in sub


class TestNoMutatingAxiSelfUpdate:
    """The mutating ``cwcli axi self-update`` is deliberately deferred; only the
    READ-ONLY ``--check`` form ships. ``--check`` is a REQUIRED option, so the verb
    can never upgrade the tool an agent is executing from mid-session. This test
    keeps the mutating path unreachable a decision, not an oversight."""

    def test_axi_self_update_requires_check(self):
        # `self-update` is registered, but `--check` is a required typer.Option, so
        # there is no mutating path - the OptionInfo carries the Ellipsis sentinel.
        import inspect

        cmd = next(c for c in axi_mod.app.registered_commands if c.name == "self-update")
        check_default = inspect.signature(cmd.callback).parameters["check"].default
        assert "--check" in check_default.param_decls
        assert check_default.default is ...

    def test_axi_self_update_without_check_is_usage_error(self):
        result = runner.invoke(axi_mod.app, ["self-update"])
        assert result.exit_code == 2
        assert "--check" in result.stdout


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
