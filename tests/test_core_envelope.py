"""Envelope / typed-error / choice DTO contract + core UI-purity bans.

Covers tasks 1.3, 1.4, and 2.4: the DTOs serialize to plain data with no live
object, the enums round-trip their string values, and NO module under ``core/``
pulls ``rich`` / ``questionary`` / ``typer`` into the process at import time (so
it cannot prompt or call ``typer.Exit``) or imports ``confirm_or_exit``.

The UI-import ban is checked **transitively**: a core module is a violation if
ANY path of load-time imports from it reaches a banned root, not only its own
direct imports. That closes the hole where a core module imports a UI-free
helper out of a util that itself imports ``typer``/``rich`` at load time (e.g.
the old ``core -> utils.docker_utils -> typer`` and
``core -> utils.port_utils -> utils.console -> rich`` paths) - which made the
"core imports no UI" claim only directly, not literally, true. A new core
module that reaches UI through any such util is now caught here rather than
silently pulling UI into the process.
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
_PKG_DIR = _CORE_DIR.parent
_PKG_ROOT = _PKG_DIR.name  # "caffeinated_whale_cli"
_BANNED_IMPORT_ROOTS = {"rich", "questionary", "typer"}


def _core_modules():
    return sorted(p for p in _CORE_DIR.glob("*.py"))


def _module_key(path: pathlib.Path) -> str:
    """Dotted module name relative to the package root (``core.docker``, ``utils``)."""
    rel = path.relative_to(_PKG_DIR).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _toplevel_import_nodes(tree: ast.Module):
    """Import nodes that execute at MODULE LOAD - i.e. not nested inside a function.

    Load-time imports are exactly what "importing this module pulls into the
    process". An import lazily performed inside a function body does not run on
    import, so it is not a load-time taint (and is how a mixed util keeps its UI
    printing out of its import surface). Class-body imports DO run at load, so
    they are kept.
    """
    lazy: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    lazy.add(id(sub))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in lazy:
            yield node


def _resolve_relative(pkg_parts: list[str], level: int, module: str | None) -> str | None:
    """Resolve a ``from ..x import y`` base module to a package-relative dotted key.

    ``pkg_parts`` is the importing module's package, relative to the package root
    (e.g. ``["core"]`` for ``core.init``). ``level`` counts dots: level 1 is that
    package, and a module at depth D can go up to level D+1 (the package root, an
    empty base). Anything deeper escapes the package.
    """
    if level - 1 > len(pkg_parts):
        return None
    base = pkg_parts[: len(pkg_parts) - (level - 1)]
    if module:
        base = base + module.split(".")
    return ".".join(base)


def _build_graph():
    """Map each internal module -> (internal import edges, banned roots it imports).

    Covers the whole package so the transitive walk can traverse ``utils`` on its
    way from a core module to a banned root.
    """
    modules = {_module_key(p): p for p in _PKG_DIR.rglob("*.py")}
    edges: dict[str, set[str]] = {}
    banned: dict[str, set[str]] = {}

    for key, path in modules.items():
        pkg_parts = key.split(".")[:-1] if "." in key else ([] if key == "" else [])
        # For a package __init__ (key has no trailing module) pkg_parts is the key itself;
        # rglob never yields "" here since the root __init__ maps to "". Treat key parts as
        # the package for __init__ files.
        if path.name == "__init__.py":
            pkg_parts = key.split(".") if key else []
        else:
            pkg_parts = key.split(".")[:-1]

        tree = ast.parse(path.read_text())
        out: set[str] = set()
        bad: set[str] = set()
        for node in _toplevel_import_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in _BANNED_IMPORT_ROOTS:
                        bad.add(root)
                    if root == _PKG_ROOT:
                        internal = alias.name[len(_PKG_ROOT) + 1 :]
                        if internal in modules:
                            out.add(internal)
            else:  # ImportFrom
                if node.level > 0:
                    base = _resolve_relative(pkg_parts, node.level, node.module)
                    if base is None:
                        continue
                elif node.module and node.module.split(".")[0] == _PKG_ROOT:
                    base = node.module[len(_PKG_ROOT) + 1 :]
                elif node.module:
                    root = node.module.split(".")[0]
                    if root in _BANNED_IMPORT_ROOTS:
                        bad.add(root)
                    continue
                else:
                    continue
                # `from <base> import <name>`: importing binds <base> (its __init__)
                # and any <base>.<name> submodule, so both are load-time edges.
                if base in modules:
                    out.add(base)
                for alias in node.names:
                    sub = f"{base}.{alias.name}" if base else alias.name
                    if sub in modules:
                        out.add(sub)
        edges[key] = out
        banned[key] = bad
    return edges, banned


def _banned_path(start: str, edges, banned) -> list[str] | None:
    """Return an import path start -> ... -> module-that-imports-banned, or None."""
    seen = {start}
    stack: list[tuple[str, list[str]]] = [(start, [start])]
    while stack:
        node, path = stack.pop()
        if banned.get(node):
            return path
        for nxt in edges.get(node, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append((nxt, path + [nxt]))
    return None


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
    def test_no_core_module_imports_ui_directly(self):
        offenders = {}
        for module in _core_modules():
            roots = _imported_roots(module.read_text())
            banned = roots & _BANNED_IMPORT_ROOTS
            if banned:
                offenders[module.name] = banned
        assert not offenders, f"core modules import UI packages: {offenders}"

    def test_no_core_module_reaches_ui_transitively(self):
        """No load-time import path from a core module may reach a banned UI root.

        This is the airtight form of the ban: it follows the intra-package import
        graph, so a core module tainted THROUGH a util (which itself imports
        ``typer``/``rich``) is caught here, not only a directly-importing one.
        """
        edges, banned = _build_graph()
        offenders = {}
        for module in _core_modules():
            key = _module_key(module)
            path = _banned_path(key, edges, banned)
            if path is not None:
                roots = banned[path[-1]]
                offenders[key] = f"{' -> '.join(path)} imports {sorted(roots)}"
        assert not offenders, f"core modules reach UI transitively: {offenders}"

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
