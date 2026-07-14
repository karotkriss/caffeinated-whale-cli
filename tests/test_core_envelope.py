"""Envelope / typed-error / choice DTO contract + core UI-purity bans.

Covers tasks 1.3, 1.4, and 2.4: the DTOs serialize to plain data with no live
object, the enums round-trip their string values, and NO module under ``core/``
imports ``rich`` / ``questionary`` / ``typer`` (so it cannot prompt or call
``typer.Exit``) or ``confirm_or_exit``.
"""

import ast
import dataclasses
import pathlib

import pytest

from caffeinated_whale_cli.core import resolvers
from caffeinated_whale_cli.core.backup import BackupOutcome
from caffeinated_whale_cli.core.envelope import Choice, Message, Result, Status
from caffeinated_whale_cli.core.errors import CwcliError, ErrorKind

_CORE_DIR = pathlib.Path(resolvers.__file__).parent
_BANNED_IMPORT_ROOTS = {"rich", "questionary", "typer"}


def _core_modules():
    return sorted(p for p in _CORE_DIR.glob("*.py"))


def _imported_roots(source: str) -> set[str]:
    roots: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _imported_names(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
    return names


class TestCorePurity:
    def test_no_core_module_imports_ui(self):
        offenders = {}
        for module in _core_modules():
            roots = _imported_roots(module.read_text())
            banned = roots & _BANNED_IMPORT_ROOTS
            if banned:
                offenders[module.name] = banned
        assert not offenders, f"core modules import UI packages: {offenders}"

    def test_no_core_module_imports_confirm_or_exit(self):
        # confirm_or_exit is CLI-only (spec: core-io-resolvers). If it is never
        # imported it can never be called from the core.
        for module in _core_modules():
            assert "confirm_or_exit" not in _imported_names(module.read_text()), module.name

    def test_no_core_module_calls_typer_exit_in_source(self):
        # Belt-and-suspenders on top of the import ban: no `typer.Exit(` in code
        # (docstrings/comments are stripped by parsing to AST and re-checking calls).
        for module in _core_modules():
            tree = ast.parse(module.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "Exit":
                    value = node.value
                    assert not (
                        isinstance(value, ast.Name) and value.id == "typer"
                    ), f"{module.name} references typer.Exit"


class TestEnvelopeDTOs:
    def test_result_asdict_is_plain_data(self):
        result = Result(
            status=Status.OK,
            data=BackupOutcome(
                site="a.localhost",
                bench_path="/workspace/frappe-bench",
                artifact_path="/x/y.sql.gz",
                included_files=False,
            ),
        )
        blob = dataclasses.asdict(result)
        assert blob["data"] == {
            "site": "a.localhost",
            "bench_path": "/workspace/frappe-bench",
            "artifact_path": "/x/y.sql.gz",
            "included_files": False,
        }
        # The outcome DTO is plain data - every leaf a builtin scalar/None, no live
        # Docker object (asdict leaves the envelope's Status enum as-is; the TOON
        # serializer converts enums at the output boundary).
        _assert_json_safe(blob["data"])

    def test_status_roundtrips_string_values(self):
        for status in Status:
            assert Status(status.value) is status
        assert Status.NEEDS_CHOICE.value == "needs_choice"

    def test_errorkind_roundtrips_string_values(self):
        for kind in ErrorKind:
            assert ErrorKind(kind.value) is kind
        assert ErrorKind.USAGE.value == "usage"

    def test_choice_carries_fillable_param(self):
        choice = Choice(
            kind="select_bench",
            param="bench",
            prompt="pick one",
            options=[{"value": "0", "label": "/w/b0"}],
        )
        assert choice.param == "bench"
        assert choice.options[0]["value"] == "0"

    def test_message_detail_optional(self):
        """A Message's detail defaults to None and round-trips when set."""
        assert Message("code", "text").detail is None
        assert Message("code", "text", {"k": 1}).detail == {"k": 1}

    def test_result_is_frozen(self):
        """Result is frozen - reassigning a field raises."""
        result = Result(status=Status.OK)
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.status = Status.WARNING  # type: ignore[misc]

    def test_result_has_no_errors_field(self):
        assert "errors" not in {f.name for f in dataclasses.fields(Result)}


class TestCwcliError:
    def test_carries_kind_code_message_hint_detail(self):
        err = CwcliError(ErrorKind.NOT_FOUND, "x.missing", "not here", hint="do y", detail={"o": 1})
        assert err.kind is ErrorKind.NOT_FOUND
        assert err.code == "x.missing"
        assert err.message == "not here"
        assert err.hint == "do y"
        assert err.detail == {"o": 1}
        assert str(err) == "not here"


def _assert_json_safe(value):
    if isinstance(value, dict):
        for v in value.values():
            _assert_json_safe(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _assert_json_safe(v)
    else:
        assert value is None or isinstance(value, (str, int, float, bool)), type(value)
