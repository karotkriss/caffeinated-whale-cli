"""Bench labeling model, selector resolution, and marker-file I/O.

This module owns the multi-bench addressing scheme used by ``--bench`` and the
``label`` command. It has two halves:

Pure helpers (no container needed, trivially unit-testable):
  - ``validate_user_label`` / ``is_numeric_label`` - the label rules.
  - ``resolve_bench`` - resolve a ``<number|label>`` selector against a list of
    cached bench dicts.
  - ``format_bench_list`` - a human list of ``index | label | path`` for errors.

Marker-file I/O (needs a running frappe container, exercised via a fake in tests):
  - ``read_label_marker`` / ``write_label_marker`` / ``clear_label_marker`` -
    read/write ``<bench-root>/.cwcli/.bench-label`` INSIDE the container.

Label model
-----------
Every discovered bench has a **numeric label** equal to its index in stable,
sorted discovery order (0, 1, 2, ...). Numeric labels are positional: adding or
removing a bench can renumber the rest, so a user label is the durable handle.

A bench may also have an optional **user label**. A ``--bench`` selector matches a
user label first, then falls back to a numeric index. To keep that unambiguous a
user label may not be purely numeric (it would collide with an index) and must be
unique within its project (uniqueness is enforced by the caller, which has the
sibling benches).

Marker file
-----------
The user label is persisted both in the SQLite cache AND in a per-bench marker at
``<bench-root>/.cwcli/.bench-label`` so labels can be rebuilt from the live benches
if the cache is lost. The marker is JSON (room for future fields)::

    {"schema": 1, "label": "staging"}
"""

import base64
import json
import re
import shlex

# Relative path of the marker inside each bench root.
MARKER_DIR = ".cwcli"
MARKER_FILENAME = ".bench-label"
MARKER_REL_PATH = f"{MARKER_DIR}/{MARKER_FILENAME}"

# Current marker schema version, written into every marker for forward-compat.
MARKER_SCHEMA = 1

# Allowed user-label charset: letters, digits, dot, dash, underscore. This keeps
# labels safe to embed in shell commands (the marker write) and in display, and
# avoids whitespace/metacharacter surprises.
_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_LABEL_LEN = 64


def is_numeric_label(value: str) -> bool:
    """True if ``value`` is a non-empty run of ASCII digits (a numeric index).

    ASCII-only on purpose: ``str.isdigit()`` also matches Unicode digit-like
    characters (``²``, ``٣``, ``①``, ...) that ``int()`` cannot parse, so a bare
    ``isdigit()`` would classify them as numeric and then crash ``resolve_bench``
    on ``int(selector)``. Gating on ``isascii()`` keeps such selectors out of the
    numeric-index path (they fall through to the no-match result instead).
    """
    return value.isascii() and value.isdigit()


def validate_user_label(label: str) -> str | None:
    """Validate a candidate user label. Returns an error string, or None if valid.

    Rules (see module docstring): non-empty, within length, allowed charset, and
    NOT purely numeric (that would collide with a bench's numeric index).
    Duplicate-label detection is the caller's job (it needs the sibling benches).
    """
    if label is None or label == "":
        return "Label cannot be empty (use --clear to remove a label)."
    if len(label) > _MAX_LABEL_LEN:
        return f"Label is too long (max {_MAX_LABEL_LEN} characters)."
    if is_numeric_label(label):
        return (
            "A label cannot be purely numeric - numeric selectors are reserved "
            "for bench indices (0, 1, 2, ...). Pick a name like 'staging'."
        )
    if not _LABEL_RE.match(label):
        return "Label may only contain letters, digits, dot (.), dash (-), and underscore (_)."
    return None


def resolve_bench(bench_instances: list[dict], selector: str) -> dict | None:
    """Resolve a ``--bench`` selector to a single bench dict, or None if no match.

    Resolution order (see module docstring):
      1. exact user-label match (case-sensitive),
      2. else, if the selector is all digits, the bench at that numeric index,
      3. else no match.

    Labels are tried before indices, so a labeled bench is reachable even if some
    unusual cache still held a numeric label; new labels can't be numeric, so the
    two namespaces do not overlap in practice.
    """
    if selector is None:
        return None
    selector = str(selector)

    for bench in bench_instances:
        if bench.get("label") and bench["label"] == selector:
            return bench

    if is_numeric_label(selector):
        index = int(selector)
        if 0 <= index < len(bench_instances):
            return bench_instances[index]

    return None


def format_bench_list(bench_instances: list[dict]) -> str:
    """Render benches as ``[index] label -> path`` lines for error/help messages."""
    lines = []
    for index, bench in enumerate(bench_instances):
        label = bench.get("label")
        label_part = f"'{label}' " if label else ""
        lines.append(f"  [{index}] {label_part}{bench.get('path', '?')}")
    return "\n".join(lines)


def _marker_path(bench_path: str) -> str:
    """Absolute marker path for a bench root (POSIX; the bench lives in Linux)."""
    return f"{bench_path.rstrip('/')}/{MARKER_REL_PATH}"


def read_label_marker(container, bench_path: str, verbose: bool = False) -> str | None:
    """Read the user label from a bench's marker file inside the container.

    Returns the label string, or None when the marker is absent, unreadable, or
    malformed (fail-safe: a bad marker simply means "no recovered label", never an
    error). ``container`` only needs an ``exec_run(cmd)`` method.
    """
    marker = _marker_path(bench_path)
    try:
        exit_code, output = container.exec_run(["cat", marker])
    except Exception:
        return None
    if exit_code != 0:
        return None

    raw = output[0] if isinstance(output, tuple) else output
    if isinstance(raw, (bytes, bytearray)):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    label = data.get("label")
    if isinstance(label, str) and label:
        return label
    return None


def write_label_marker(container, bench_path: str, label: str, verbose: bool = False) -> bool:
    """Write ``{"schema": 1, "label": <label>}`` to the bench's marker file.

    The JSON is base64-encoded on the host and decoded in the container, so no
    label content is ever interpolated into a shell command. Returns True on a
    zero-exit write. ``container`` needs ``exec_run(cmd)``.
    """
    marker = _marker_path(bench_path)
    marker_dir = f"{bench_path.rstrip('/')}/{MARKER_DIR}"
    payload = json.dumps({"schema": MARKER_SCHEMA, "label": label})
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    script = (
        f"mkdir -p {shlex.quote(marker_dir)} && "
        f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(marker)}"
    )
    try:
        exit_code, _ = container.exec_run(["sh", "-c", script])
    except Exception:
        return False
    return bool(exit_code == 0)


def clear_label_marker(container, bench_path: str, verbose: bool = False) -> bool:
    """Remove a bench's marker file. Returns True on a zero-exit ``rm -f``.

    ``rm -f`` is a no-op (still zero exit) when the marker is already absent.
    """
    marker = _marker_path(bench_path)
    try:
        exit_code, _ = container.exec_run(["rm", "-f", marker])
    except Exception:
        return False
    return bool(exit_code == 0)
