"""Typed error hierarchy the logic core raises for hard failures.

The core raises :class:`CwcliError` when it genuinely cannot proceed; each
frontend has ONE handler mapping ``kind`` to an exit code and a rendering. Soft,
partial, or expected outcomes never raise - they ride in
:class:`~.envelope.Result` ``data`` + ``warnings`` with an ``OK``/``WARNING``
status (there is deliberately no ``errors`` field on ``Result``).
"""

from __future__ import annotations

from enum import Enum


class ErrorKind(Enum):
    """Closed set of hard-failure kinds, each mapping to a frontend exit code.

    The ``axi`` frontend maps :attr:`USAGE` to exit 2 and every other kind to
    exit 1 (mirroring the ``typer.Exit(1/2)`` conventions already in the code).
    """

    USAGE = "usage"  # bad/missing/mutually-exclusive flag
    NOT_FOUND = "not_found"  # project/site/bench absent
    NOT_RUNNING = "not_running"  # container down, no auto-start
    CONFLICT = "conflict"  # port conflict, existing bench
    PRECONDITION = "precondition"  # a gate failed (backup dump, disk, ...)
    DOCKER = "docker"  # daemon down / API error
    INTERNAL = "internal"  # unexpected


class CwcliError(Exception):
    """A hard failure the core cannot recover from.

    Carries a machine ``kind`` (:class:`ErrorKind`), a stable ``code`` token, a
    human ``message``, an optional ``hint`` (an axi-facing "pass --X"
    suggestion), and optional ``detail`` (e.g. captured command output a
    frontend may echo in verbose mode). Frontends catch it, map ``kind`` -> exit
    code, and render ``message`` (plus ``hint`` where present) in their own style.
    """

    def __init__(
        self,
        kind: ErrorKind,
        code: str,
        message: str,
        hint: str | None = None,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.message = message
        self.hint = hint
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"CwcliError(kind={self.kind.value!r}, code={self.code!r}, message={self.message!r})"
