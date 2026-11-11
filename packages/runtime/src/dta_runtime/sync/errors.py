"""Errors raised by the sync pipeline, each with a stable code under `sync.`."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from dta_core.errors import DtaError

MAPPING_INVALID = "sync.mapping_invalid"
UNIT_MISMATCH = "sync.unit_mismatch"
SOURCE_FAILED = "sync.source_failed"
ALL_REJECTED = "sync.all_rejected"
STORE_FAILED = "sync.store_failed"
REJECTS_UNWRITABLE = "sync.rejects_unwritable"
UNEXPECTED = "sync.unexpected"
REPLAY_UNSUPPORTED = "sync.replay_unsupported"
REPLAY_UNKNOWN = "sync.replay_unknown"
UNKNOWN_SOURCE = "sync.unknown_source"
# A source that was read but is skipped because what it joins could not be.
DEPENDENCY_FAILED = "sync.dependency_failed"
# Recorded with a run in which some sources failed and the others were synced.
PARTIAL = "sync.partial"


@dataclass(frozen=True)
class Problem:
    """One reason the manifest's sources cannot be loaded as written."""

    code: str
    # Dotted path of the manifest field at fault, such as `sources.scada.mappings.tanks`.
    where: str
    message: str


class SyncError(DtaError):
    """Base class of every error the sync pipeline raises."""

    code = "sync.error"


class MappingInvalid(SyncError):
    """The mappings, views or sync settings cannot be used; `problems` lists every reason."""

    code = MAPPING_INVALID

    def __init__(self, problems: list[Problem], *, detail: dict[str, Any] | None = None) -> None:
        first = problems[0]
        suffix = "" if len(problems) == 1 else f" (and {len(problems) - 1} more)"
        super().__init__(
            f"{first.where}: {first.message}{suffix}",
            detail={**(detail or {}), "problems": [asdict(problem) for problem in problems]},
        )
        self.problems = list(problems)


class UnitMismatch(MappingInvalid):
    """Every problem found is a unit that is unknown or cannot be converted."""

    code = UNIT_MISMATCH


class SourceFailed(SyncError):
    """A source could not be read; nothing of the run was written but the failure record."""

    code = SOURCE_FAILED


class AllRejected(SyncError):
    """Every row of at least one mapping was set aside."""

    code = ALL_REJECTED


class StoreFailed(SyncError):
    """A version could not be written, or it cannot be told whether it was."""

    code = STORE_FAILED


class ReplayUnsupported(SyncError):
    """The dataset named for a replay feeds a state series, which cannot be read again:
    a state property holds the latest value and nothing can be written behind it."""

    code = REPLAY_UNSUPPORTED


def plan_error(problems: list[Problem]) -> MappingInvalid:
    """The error reporting `problems` together, under the code that describes all of them."""
    if all(problem.code == UNIT_MISMATCH for problem in problems):
        return UnitMismatch(problems)
    return MappingInvalid(problems)
