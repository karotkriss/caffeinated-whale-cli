"""A small, dependency-free TOON encoder for the ``cwcli axi`` stdout boundary.

TOON (Token-Oriented Object Notation, https://toonformat.dev) is the agent-facing
output format: objects are ``key: value`` lines, uniform record collections are
tabular ``name[N]{fields}:`` blocks, and lists of strings are block lines. JSON
stays internal (``dataclasses.asdict`` -> plain dicts); this module only ever
writes at the output boundary, which bounds its surface to encoding.

Run this module directly for a self-check that the encoding matches the JSON-shaped
data the codebase already produces: ``python -m caffeinated_whale_cli.utils.toon``.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

_SPECIAL = set(",:\"'\n\r")


def _scalar(value) -> str:
    """Render a single scalar as a TOON token (quoting only when needed)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Enum):
        return _scalar(value.value)
    if isinstance(value, Path):
        return _scalar(str(value))
    text = str(value)
    if _needs_quote(text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _needs_quote(text: str) -> bool:
    """A string needs quoting if it is empty, has edge whitespace/special chars, or
    would be misread as a number/bool/null."""
    if text == "":
        return True
    if text != text.strip():
        return True
    if any(ch in _SPECIAL for ch in text):
        return True
    if text in ("true", "false", "null"):
        return True
    # A bare number-looking string must be quoted so it is not read as a number.
    try:
        float(text)
        return True
    except ValueError:
        return False


def kv(key: str, value) -> str:
    """A single ``key: value`` line."""
    return f"{key}: {_scalar(value)}"


def table(name: str, rows: list[dict], fields: list[str]) -> str:
    """A tabular ``name[N]{fields}:`` block; each row lists ``fields`` in order."""
    header = f"{name}[{len(rows)}]{{{','.join(fields)}}}:"
    lines = [header]
    for row in rows:
        lines.append("  " + ",".join(_scalar(row.get(f)) for f in fields))
    return "\n".join(lines)


def block(name: str, items: list[str]) -> str:
    """A ``name[N]:`` block with one raw string item per indented line (e.g. help)."""
    lines = [f"{name}[{len(items)}]:"]
    lines.extend(f"  {item}" for item in items)
    return "\n".join(lines)


def _encode_value(key: str, value, indent: int) -> list[str]:
    pad = "  " * indent
    if isinstance(value, dict):
        out = [f"{pad}{key}:"]
        out.extend(_encode_dict(value, indent + 1))
        return out
    if isinstance(value, list):
        if value and all(isinstance(v, dict) for v in value):
            fields = list(value[0].keys())
            tbl = table(key, value, fields).split("\n")
            return [pad + tbl[0]] + [pad + line for line in tbl[1:]]
        # list of scalars -> inline
        rendered = ",".join(_scalar(v) for v in value)
        return [f"{pad}{key}[{len(value)}]: {rendered}" if value else f"{pad}{key}[0]:"]
    return [f"{pad}{kv(key, value)}"]


def _encode_dict(data: dict, indent: int) -> list[str]:
    lines: list[str] = []
    for key, value in data.items():
        lines.extend(_encode_value(str(key), value, indent))
    return lines


def encode(data: dict, *, warnings: list | None = None) -> str:
    """Encode a plain (``asdict``-shaped) dict as a TOON document.

    ``warnings`` is an optional list of ``Message``-shaped dataclasses/dicts; when
    present it is appended as a ``warnings[N]:`` block of their ``text``.
    """
    lines = _encode_dict(data, 0)
    if warnings:
        texts: list[str] = []
        for warning in warnings:
            if isinstance(warning, dict):
                texts.append(str(warning.get("text", "")))
            else:
                texts.append(str(getattr(warning, "text", "")))
        lines.append(block("warnings", texts))
    return "\n".join(lines)


def _self_check() -> None:
    # A flat outcome dict encodes to key:value lines matching its JSON shape.
    outcome = {
        "site": "development.localhost",
        "bench_path": "/workspace/frappe-bench",
        "artifact_path": "/workspace/frappe-bench/sites/development.localhost/private/backups/x.sql.gz",
        "included_files": False,
    }
    doc = encode(outcome)
    assert "site: development.localhost" in doc
    assert "included_files: false" in doc, doc

    # A uniform record collection encodes to a tabular block.
    rows = [
        {"projectName": "proj-a", "status": "running", "ports": "8000 8001"},
        {"projectName": "proj-b", "status": "exited", "ports": ""},
    ]
    tbl = table("instances", rows, ["projectName", "status", "ports"])
    assert tbl.splitlines()[0] == "instances[2]{projectName,status,ports}:", tbl
    assert "  proj-a,running,8000 8001" in tbl, tbl
    # An empty cell is quoted so it is unambiguous.
    assert '  proj-b,exited,""' in tbl, tbl

    # Numeric-looking strings are quoted so they aren't read as numbers.
    assert _scalar("42") == '"42"'
    assert _scalar(42) == "42"
    assert _scalar(True) == "true"

    # help block: one raw command per line.
    b = block("help", ["Run `cwcli axi backup <project>`", "Run `cwcli ls`"])
    assert b.splitlines()[0] == "help[2]:"
    assert b.splitlines()[1] == "  Run `cwcli axi backup <project>`"

    print("toon self-check OK")


if __name__ == "__main__":
    _self_check()
