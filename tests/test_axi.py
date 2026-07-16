"""``cwcli axi`` surface: TOON serializer, verb exit-mapping, content-first home.

Covers task 4.5. The core is stubbed (so these exercise the axi rendering / exit
mapping in isolation): TOON on stdout for success, no progress text on stdout,
typed-error rendering + exit codes, needs-choice -> flag-naming usage error exit
2, and the ls-DTO home populated + empty. Plus the shared TOON encoder's shape.
"""

import re

import pytest
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
            ),
            WhereMatch(type="site", project="proj-a", bench="/w/b", name="s.localhost"),
        ]
        monkeypatch.setattr(
            axi_mod.core_where,
            "where",
            lambda *a, **k: Result(status=Status.OK, data=WhereResult(matches=matches)),
        )
        result = runner.invoke(axi_mod.app, ["where", "erp"])
        assert result.exit_code == 0
        assert result.stdout.startswith(
            "matches[2]{type,project,bench,name,version,branch,site,installed}:"
        )
        assert "app,proj-a,/w/b,erpnext,15.0.0,version-15,s.localhost,true" in result.stdout
        assert "site,proj-a,/w/b,s.localhost,null,null,null,false" in result.stdout

    def test_where_definitive_empty_state(self, monkeypatch):
        monkeypatch.setattr(
            axi_mod.core_where,
            "where",
            lambda *a, **k: Result(status=Status.OK, data=WhereResult(matches=[])),
        )
        result = runner.invoke(axi_mod.app, ["where", "zzz"])
        assert result.exit_code == 0
        # An empty typed collection is still a definitive TOON empty state.
        assert result.stdout.strip() == "matches[0]:"

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

    def test_self_check_runs(self):
        """The toon module's internal self-check passes."""
        toon._self_check()  # asserts internally; must not raise


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
