"""The serializable, typed return contract every core function speaks.

Modeled on the shape ``commands/apps.py:_report_and_exit`` already hand-rolled
(a ``results`` collection + an aggregate reporter), lifted into typed stdlib
dataclasses. Every DTO reachable from :class:`Result` serializes to plain nested
data via :func:`dataclasses.asdict` and holds no live Docker object, so the same
value feeds the CLI, ``cwcli axi``, and a future GUI unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, TypeVar

T = TypeVar("T")


class Status(Enum):
    """The outcome status of a core call."""

    OK = "ok"  # completed
    WARNING = "warning"  # completed with non-fatal issues (partial fan-out, no-op)
    NEEDS_CHOICE = "needs_choice"  # cannot proceed without a decision the core won't make


@dataclass(frozen=True, slots=True)
class Message:
    """A non-fatal, machine-tokened note carried in ``Result.warnings``."""

    code: str  # stable token, e.g. "default_site.resolved", "bench.default_used"
    text: str
    detail: dict | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Choice:
    """A decision the core cannot make from its params; the frontend resolves it.

    ``param`` names the core parameter the frontend must fill to re-invoke, so a
    re-invocation with that value supplied proceeds without emitting the same
    choice again.
    """

    kind: str  # machine token: "select_bench" | "confirm_start" | ...
    param: str  # the core param to fill on re-invoke, e.g. "bench"
    prompt: str  # human question
    options: list[dict] | None = None  # selects: [{"value": "0", "label": ".../frappe-bench"}]
    default: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Result(Generic[T]):
    """The common envelope. ``data`` is the command's typed outcome DTO."""

    status: Status
    data: T | None = None
    warnings: list[Message] = field(default_factory=list)
    choice: Choice | None = None  # set iff status is NEEDS_CHOICE
