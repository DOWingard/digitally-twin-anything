"""What a function gives back, and what code of the twin's own is handed to read the twin.

A function, shipped or registered, returns an `Answer` (or a bare value, which
is an answer without unit, interval or notes), or `Derived` rows to be written
to the twin. The API adds what only it knows: the version that was read,
whether the question is provisional, the function and the model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import pyarrow as pa
from dta_core.schema import FactKind

if TYPE_CHECKING:
    from .worker import FrameRef


@dataclass(frozen=True)
class Interval:
    """Where the value lies with the stated coverage, in the unit of the value."""

    low: float
    high: float
    # Coverage in percent, such as 95.
    level: float


@dataclass(frozen=True)
class Answer:
    # A JSON value (number, text, list, mapping) or a table. A table that carries intervals
    # holds them as columns `lo_<level>` and `hi_<level>` beside the value.
    value: Any
    # UCUM code of the value, or of a table's value column.
    unit: str | None = None
    # For a single number only.
    interval: Interval | None = None
    # What the reader has to know to use the value, as short codes: `outside_validity`
    # (asked beyond what the model saw), `truncated` (more rows exist than returned).
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Derived:
    """Rows a function wants written to the twin: values it worked out, never observed ones.

    The API writes them in one commit, with the function as their lineage, and refuses
    a property that a source fills.
    """

    type: str
    # One row per object: `uid` and a column per property written.
    rows: pa.Table
    kind: FactKind
    confidence: float | None = None


@runtime_checkable
class Reader(Protocol):
    """The twin as code of its own sees it: read-only, at one version of one branch."""

    # Id of the version every read is made at, and its branch.
    at: str
    branch: str

    def get(self, path: str, **params: Any) -> Any:
        """The JSON document of `GET /api/v1<path>` at the reader's version."""
        ...

    def table(self, path: str, **params: Any) -> pa.Table:
        """The table of `GET /api/v1<path>` at the reader's version, whole."""
        ...

    def frame(self, ref: FrameRef) -> pa.Table:
        """The frame a reference names: `unique_id`, `ds`, `y` and its features, over the
        versions the reference states (not the reader's own)."""
        ...


@runtime_checkable
class Artifacts(Protocol):
    """Files the twin keeps by the SHA-256 of their content: code, fitted states, tables."""

    def get(self, sha256: str) -> bytes: ...

    def put(self, content: bytes, media_type: str) -> str:
        """Store the content; its SHA-256."""
        ...


def answer_document(
    answer: Answer,
    *,
    version: Mapping[str, Any],
    function: str,
    model: str | None,
    provisional: bool,
    reasons: Sequence[str] = (),
) -> dict[str, Any]:
    """The JSON document of an answer whose value is not a table."""
    if isinstance(answer.value, pa.Table):
        raise TypeError("a table is served as a table, with this document in its metadata")
    interval = answer.interval
    return {
        "value": answer.value,
        "unit": answer.unit,
        "interval": (
            {"low": interval.low, "high": interval.high, "level": interval.level}
            if interval is not None
            else None
        ),
        "notes": list(answer.notes),
        "version": dict(version),
        "function": function,
        "model": model,
        "provisional": provisional,
        # Why the answer is provisional; empty when it is not.
        "reasons": list(reasons),
    }
