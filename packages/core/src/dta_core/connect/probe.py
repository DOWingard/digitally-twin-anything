"""A quick look at every dataset of a source: can it be read, and what are its columns."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..errors import DtaError
from ..log import Log
from ..sources import Connector

log = Log("connect.probe")

PROBE_ROWS = 5


@dataclass(frozen=True)
class ProbeColumn:
    name: str
    type: str  # Arrow type as text, after normalisation


@dataclass(frozen=True)
class DatasetProbe:
    dataset: str
    ok: bool
    has_rows: bool = False
    columns: tuple[ProbeColumn, ...] = ()
    error_code: str | None = None
    error: str | None = None
    # Fields the rows held that the dataset's declared columns leave out, so never read.
    dropped_columns: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "ok": self.ok,
            "has_rows": self.has_rows,
            "columns": [{"name": c.name, "type": c.type} for c in self.columns],
            "dropped_columns": list(self.dropped_columns),
            "error_code": self.error_code,
            "error": self.error,
        }


@dataclass(frozen=True)
class ProbeReport:
    source_id: str
    datasets: tuple[DatasetProbe, ...] = field(default_factory=tuple)
    # Set when the source could not even list its datasets.
    error_code: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_code is None and all(probe.ok for probe in self.datasets)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "ok": self.ok,
            "error_code": self.error_code,
            "error": self.error,
            "datasets": [probe.as_dict() for probe in self.datasets],
        }


def probe(connector: Connector) -> ProbeReport:
    """Read the first rows of every dataset; a failing dataset does not stop the others.

    Errors are reported by code and message, which connectors keep free of
    secrets; row values are neither returned nor logged.
    """
    started = time.perf_counter()
    source_id = connector.source_id
    try:
        names = connector.datasets()
    except DtaError as exc:
        log.warn("probe could not list datasets", source=source_id, code=exc.code)
        return ProbeReport(source_id, (), exc.code, exc.message)
    results = tuple(_probe_dataset(connector, name) for name in names)
    report = ProbeReport(source_id, results)
    log.info(
        "probe finished",
        source=source_id,
        datasets=len(results),
        failed=[probe.dataset for probe in results if not probe.ok],
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return report


def _probe_dataset(connector: Connector, name: str) -> DatasetProbe:
    started = time.perf_counter()
    try:
        tables = list(connector.read(name, limit=PROBE_ROWS))
    except DtaError as exc:
        log.warn(
            "probe of dataset failed",
            source=connector.source_id,
            dataset=name,
            code=exc.code,
        )
        return DatasetProbe(name, ok=False, error_code=exc.code, error=exc.message)
    schema = tables[0].schema
    rows = sum(table.num_rows for table in tables)
    # Only a connector that keeps a declared list of columns can leave fields out.
    dropped_of = getattr(connector, "dropped_columns", None)
    dropped = tuple(dropped_of(name)) if callable(dropped_of) else ()
    log.debug(
        "dataset probed",
        source=connector.source_id,
        dataset=name,
        rows=rows,
        columns=len(schema),
        dropped=list(dropped),
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return DatasetProbe(
        name,
        ok=True,
        has_rows=rows > 0,
        columns=tuple(ProbeColumn(f.name, str(f.type)) for f in schema),
        dropped_columns=dropped,
    )
