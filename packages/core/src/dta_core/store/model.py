"""Values passed into and returned by the store."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ..schema import FactKind
from .errors import invalid

LIVE_BRANCH = "main"
# Version time before any data has been seen.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class Cause(StrEnum):
    """Why a version was written."""

    install = "install"
    migration = "migration"
    sync = "sync"
    registration = "registration"
    action = "action"
    derived = "derived"
    manual = "manual"
    scenario = "scenario"
    merge = "merge"


# -- time ----------------------------------------------------------------------


def to_utc(value: Any, what: str) -> datetime:
    """A timezone-aware datetime, or an ISO 8601 string with an offset, as UTC."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise invalid(
                "bad_time", f"{what} is not an ISO 8601 time: {value!r}", what=what
            ) from exc
    if not isinstance(value, datetime):
        raise invalid(
            "bad_time",
            f"{what} must be a timezone-aware datetime, got {type(value).__name__}",
            what=what,
        )
    if value.tzinfo is None or value.utcoffset() is None:
        raise invalid(
            "naive_datetime",
            f"{what} has no timezone; give an aware datetime or an ISO 8601 time with an offset",
            what=what,
        )
    return value.astimezone(UTC)


def iso_utc(value: datetime) -> str:
    """`2024-05-01T12:00:00Z`, with fractional seconds only when they are not zero."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def floor_ms(value: datetime) -> datetime:
    """Commit dates keep milliseconds, so version times are cut to that precision."""
    return value.replace(microsecond=value.microsecond - value.microsecond % 1000)


# -- provenance ----------------------------------------------------------------


@dataclass(frozen=True)
class Provenance:
    """Where a stored value came from."""

    source: str  # id of the source, function or model that produced the value
    kind: FactKind
    t_valid: datetime  # when the value was true
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source:
            raise invalid("invalid_provenance", "provenance source must be a non-empty string")
        try:
            kind = FactKind(self.kind)
        except ValueError as exc:
            raise invalid(
                "invalid_provenance",
                f"provenance kind must be one of {[k.value for k in FactKind]}, got {self.kind!r}",
            ) from exc
        confidence = self.confidence
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or math.isnan(confidence)
            or not 0.0 <= confidence <= 1.0
        ):
            raise invalid(
                "invalid_provenance", f"provenance confidence must be in [0, 1], got {confidence!r}"
            )
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "confidence", float(confidence))
        object.__setattr__(self, "t_valid", to_utc(self.t_valid, "provenance t_valid"))

    def to_json(self) -> dict[str, Any]:
        return {
            "s": self.source,
            "k": self.kind.value,
            "c": self.confidence,
            "t": iso_utc(self.t_valid),
        }

    @classmethod
    def from_json(cls, document: Mapping[str, Any]) -> Provenance:
        return cls(
            source=document["s"],
            kind=FactKind(document["k"]),
            t_valid=datetime.fromisoformat(document["t"]),
            confidence=float(document["c"]),
        )


# -- writes --------------------------------------------------------------------


@dataclass
class ChangeSet:
    """Rows to write and objects to delete, applied together as one version.

    A row holds `uid` and any subset of its type's properties. A row for an
    object that does not exist yet creates it, the properties left out being
    null. A row naming every property replaces the object. A row naming some
    of them updates those and leaves the others, and their provenance, alone.
    """

    upserts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    deletes: dict[str, list[str]] = field(default_factory=dict)
    # Applies to every written row without its own entry in `row_provenance`.
    provenance: Provenance | None = None
    row_provenance: dict[str, Provenance] = field(default_factory=dict)

    def size(self) -> int:
        return sum(len(rows) for rows in self.upserts.values()) + sum(
            len(uids) for uids in self.deletes.values()
        )


@dataclass(frozen=True)
class CommitMeta:
    """What is recorded about a version besides its rows."""

    cause: Cause
    actor: str
    # Knowledge time in data time: the latest data timestamp seen when committing.
    t: datetime
    run: str | None = None
    trace: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    note: str = ""
    # Caller-defined fields, kept with the version and returned with it.
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            cause = Cause(self.cause)
        except ValueError as exc:
            raise invalid(
                "invalid_meta",
                f"cause must be one of {[c.value for c in Cause]}, got {self.cause!r}",
            ) from exc
        if not isinstance(self.actor, str) or not self.actor:
            raise invalid("invalid_meta", "actor must be a non-empty string")
        for name in ("run", "trace"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise invalid("invalid_meta", f"{name} must be a string or None")
        if not isinstance(self.note, str):
            raise invalid("invalid_meta", "note must be a string")
        object.__setattr__(self, "cause", cause)
        object.__setattr__(self, "t", floor_ms(to_utc(self.t, "version time t")))
        for name in ("valid_from", "valid_to"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, to_utc(value, name))
        object.__setattr__(self, "extra", _plain_json(self.extra))


def _plain_json(extra: Any) -> dict[str, Any]:
    """A detached copy of `extra`, refused unless it survives a JSON round trip unchanged."""
    if not isinstance(extra, Mapping):
        raise invalid("invalid_meta", f"extra must be a mapping, got {type(extra).__name__}")
    for key in extra:
        if not isinstance(key, str):
            raise invalid("invalid_meta", f"extra keys must be strings, got {key!r}")
    try:
        text = json.dumps(dict(extra), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise invalid(
            "invalid_meta", f"extra holds a value that cannot be stored as JSON: {exc}"
        ) from exc
    return json.loads(text)


# -- versions ------------------------------------------------------------------


@dataclass(frozen=True)
class VersionRef:
    """One committed version of a branch."""

    id: str  # commit id
    seq: int  # position on the branch, the installed schema being 0
    t: datetime
    branch: str
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True, kw_only=True)
class Version(VersionRef):
    """A version with everything recorded about it."""

    cause: Cause
    actor: str
    run: str | None = None
    trace: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    written_at: datetime | None = None  # wall clock, for audit only
    note: str = ""
    # First parent is the previous version of the branch; a merge has a second.
    parents: tuple[str, ...] = ()

    @property
    def ref(self) -> VersionRef:
        return VersionRef(self.id, self.seq, self.t, self.branch, self.extra)


@dataclass(frozen=True)
class BranchInfo:
    name: str
    head: str  # commit id of the newest version


@dataclass(frozen=True)
class StoredObject:
    """One object as it was at a version."""

    uid: str
    type: str
    values: dict[str, Any]
    # Keyed by property name; `*` holds the provenance of every property without its own entry.
    provenance: dict[str, Provenance]
    version: VersionRef

    def provenance_of(self, name: str) -> Provenance:
        return self.provenance.get(name) or self.provenance["*"]
