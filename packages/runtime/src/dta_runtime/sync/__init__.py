"""The sync pipeline: reads a twin's sources and writes what is new into its store.

`load` reads every source of the manifest once, maps rows to ontology objects
and writes them as versions in data time: existing history is replayed as a
sequence of versions, one per interval, and a later run continues where the
last one stopped.
"""

from .errors import (
    ALL_REJECTED,
    MAPPING_INVALID,
    SOURCE_FAILED,
    STORE_FAILED,
    UNIT_MISMATCH,
    AllRejected,
    MappingInvalid,
    Problem,
    SourceFailed,
    StoreFailed,
    SyncError,
    UnitMismatch,
)
from .pipeline import load
from .report import LoadReport, MappingCount, SourceCount

__all__ = [
    "ALL_REJECTED",
    "MAPPING_INVALID",
    "SOURCE_FAILED",
    "STORE_FAILED",
    "UNIT_MISMATCH",
    "AllRejected",
    "LoadReport",
    "MappingCount",
    "MappingInvalid",
    "Problem",
    "SourceCount",
    "SourceFailed",
    "StoreFailed",
    "SyncError",
    "UnitMismatch",
    "load",
]
