"""The sync pipeline: reads a twin's sources and writes what is new into its store.

`load` reads every source of the manifest once, maps rows to ontology objects
and writes them as versions in data time: existing history is replayed as a
sequence of versions, one per interval. `run` is what a live twin repeats
while its sources change: handed a clock, it takes the intervals that have
ended since the last run, one version each, and lets a failing source fail
alone.
"""

from .errors import (
    ALL_REJECTED,
    DEPENDENCY_FAILED,
    MAPPING_INVALID,
    REPLAY_UNKNOWN,
    REPLAY_UNSUPPORTED,
    SOURCE_FAILED,
    STORE_FAILED,
    UNIT_MISMATCH,
    UNKNOWN_SOURCE,
    AllRejected,
    MappingInvalid,
    Problem,
    ReplayUnsupported,
    SourceFailed,
    StoreFailed,
    SyncError,
    UnitMismatch,
)
from .pipeline import Replay, load, run
from .report import (
    DatasetRun,
    LoadReport,
    MappingCount,
    RunReport,
    SourceCount,
    SourceRun,
)

__all__ = [
    "ALL_REJECTED",
    "DEPENDENCY_FAILED",
    "MAPPING_INVALID",
    "REPLAY_UNKNOWN",
    "REPLAY_UNSUPPORTED",
    "SOURCE_FAILED",
    "STORE_FAILED",
    "UNIT_MISMATCH",
    "UNKNOWN_SOURCE",
    "AllRejected",
    "DatasetRun",
    "LoadReport",
    "MappingCount",
    "MappingInvalid",
    "Problem",
    "Replay",
    "ReplayUnsupported",
    "RunReport",
    "SourceCount",
    "SourceFailed",
    "SourceRun",
    "StoreFailed",
    "SyncError",
    "UnitMismatch",
    "load",
    "run",
]
