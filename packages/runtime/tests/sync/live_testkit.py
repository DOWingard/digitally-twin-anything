"""Sources that change between runs: folders of CSV files read by the real file connector.

A test writes a file, runs, appends to it or rewrites it, moves its clock and
runs again. Only a source that is to fail is stood in for, by a connector that
raises the errors a real connector raises.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
from dta_core.connect import open_connector
from dta_core.manifest.models import Source
from dta_core.sources import Connector, SourceError
from dta_core.store import Store
from dta_core.store.model import iso_utc
from sync_testkit import T0, columns

QUARTER = timedelta(minutes=15)
# The `sync` section of a twin that runs every quarter of an hour.
LIVE = {"interval": 900}
TIME = "timestamp[us, tz=UTC]"
META_TYPES = {"TwinSyncRun", "TwinDataset", "TwinSource"}


def at(minutes: float) -> datetime:
    """A time so many minutes after the twin was installed."""
    return T0 + timedelta(minutes=minutes)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class Folder:
    """One source: a folder of CSV files, one per dataset."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)

    def write(self, dataset: str, header: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
        """Replace the dataset's file: the source rewrote what it holds."""
        with (self.path / f"{dataset}.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows([_cell(value) for value in row] for row in rows)

    def append(self, dataset: str, rows: Sequence[Sequence[Any]]) -> None:
        """Add rows at the end of the dataset's file: the source gained them."""
        with (self.path / f"{dataset}.csv").open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows([_cell(value) for value in row] for row in rows)


def csv_source(
    folder: Folder,
    datasets: Mapping[str, Mapping[str, str]],
    mappings: Mapping[str, Any],
    views: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """A file source of the manifest; `datasets` gives each dataset's column types."""
    document: dict[str, Any] = {
        "connector": "file",
        "location": str(folder.path),
        "options": {
            "datasets": {
                name: {"glob": f"{name}.csv", "format": "csv", "types": dict(types)}
                for name, types in datasets.items()
            }
        },
        "mappings": dict(mappings),
    }
    if views:
        document["views"] = dict(views)
    return document


class Broken:
    """A connector whose source fails when it is read, with the error it was given."""

    def __init__(self, connector: Connector, failure: SourceError) -> None:
        self.source_id = connector.source_id
        self._connector = connector
        self._failure = failure

    def datasets(self) -> list[str]:
        return self._connector.datasets()

    def read(
        self,
        dataset: str,
        *,
        columns: Sequence[str] | None = None,
        since: Any = None,
        cursor_column: str | None = None,
        limit: int | None = None,
    ) -> Iterator[pa.Table]:
        raise self._failure

    def close(self) -> None:
        self._connector.close()

    def __enter__(self) -> Connector:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Reading:
    """A connector that notes what it is asked and passes it on to the real one."""

    def __init__(self, connector: Connector, asked: list[dict[str, Any]]) -> None:
        self.source_id = connector.source_id
        self._connector = connector
        self._asked = asked

    def datasets(self) -> list[str]:
        return self._connector.datasets()

    def read(
        self,
        dataset: str,
        *,
        columns: Sequence[str] | None = None,
        since: Any = None,
        cursor_column: str | None = None,
        limit: int | None = None,
    ) -> Iterator[pa.Table]:
        self._asked.append(
            {
                "source": self.source_id,
                "dataset": dataset,
                "since": since,
                "cursor_column": cursor_column,
            }
        )
        return self._connector.read(
            dataset, columns=columns, since=since, cursor_column=cursor_column, limit=limit
        )

    def close(self) -> None:
        self._connector.close()

    def __enter__(self) -> Connector:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Connecting:
    """Opens the real connector of each source; the sources told to fail are broken."""

    def __init__(self) -> None:
        self.failures: dict[str, SourceError] = {}
        self.opened: list[str] = []
        self.asked: list[dict[str, Any]] = []

    def __call__(self, source_id: str, source: Source, env: Mapping[str, str]) -> Connector:
        self.opened.append(source_id)
        connector = open_connector(source_id, source, env)
        failure = self.failures.get(source_id)
        if failure is not None:
            return Broken(connector, failure)
        return Reading(connector, self.asked)

    def break_(self, source_id: str, failure: SourceError) -> None:
        self.failures[source_id] = failure

    def mend(self, *source_ids: str) -> None:
        for source_id in source_ids or tuple(self.failures):
            self.failures.pop(source_id, None)


# -- a small plant: tanks with levels, pumps with speeds, sensors with readings ------

TANKS = ("id", "name", "elevation", "min_level", "max_level")
JUNCTIONS = ("id", "name", "elevation")
PUMPS = ("id", "from_node", "to_node")
SENSORS = ("id", "node", "quantity", "active")
LEVELS = ("tank", "ts", "level")
SPEEDS = ("pump", "ts", "speed")
READINGS = ("reading", "sensor", "ts", "value", "rev")


def registry_source(folder: Folder) -> dict[str, Any]:
    """Objects without a time of their own, written once to the folder."""
    folder.write(
        "tanks", TANKS, [("T1", "Hill tank", 40.0, 0.5, 6.0), ("T2", "Low tank", 20.0, 0.5, 6.0)]
    )
    folder.write("junctions", JUNCTIONS, [("J1", "North", 12.5), ("J2", "Mid", 10.0)])
    folder.write("pumps", PUMPS, [("PU1", "J1", "J2")])
    folder.write("sensors", SENSORS, [("S7", "J1", "flow", True)])
    text = {"id": "string", "name": "string"}
    return csv_source(
        folder,
        {
            "tanks": {**text, "elevation": "double", "min_level": "double", "max_level": "double"},
            "junctions": {**text, "elevation": "double"},
            "pumps": {"id": "string", "from_node": "string", "to_node": "string"},
            "sensors": {"id": "string", "node": "string", "quantity": "string", "active": "bool"},
        },
        {
            "tanks": {
                "dataset": "tanks",
                "object_type": "Tank",
                "identity": ["id"],
                "properties": columns("name", "elevation", "min_level", "max_level"),
            },
            "junctions": {
                "dataset": "junctions",
                "object_type": "Junction",
                "identity": ["id"],
                "properties": columns("name", "elevation"),
            },
            "pumps": {
                "dataset": "pumps",
                "object_type": "Pump",
                "identity": ["id"],
                "properties": columns(start_node="from_node", end_node="to_node"),
            },
            "sensors": {
                "dataset": "sensors",
                "object_type": "Sensor",
                "identity": ["id"],
                "properties": columns("quantity", "active", measures="node"),
            },
        },
    )


def levels_mapping(dataset: str = "levels") -> dict[str, Any]:
    return {
        "dataset": dataset,
        "object_type": "Tank",
        "identity": ["tank"],
        "valid_time": "ts",
        "properties": columns("level"),
    }


def levels_source(folder: Folder, rows: Sequence[Sequence[Any]] = ()) -> dict[str, Any]:
    """Tank levels as a state series, read on by its valid time."""
    folder.write("levels", LEVELS, rows)
    return csv_source(
        folder,
        {"levels": {"tank": "string", "ts": TIME, "level": "double"}},
        {"levels": levels_mapping()},
    )


def viewed_levels_source(folder: Folder, rows: Sequence[Sequence[Any]] = ()) -> dict[str, Any]:
    """The same series mapped through a view, so that its dataset is read whole every time."""
    document = levels_source(folder, rows)
    document["mappings"] = {"levels": levels_mapping("by_tank")}
    document["views"] = {"by_tank": 'SELECT tank, ts, level FROM "scada"."levels"'}
    return document


def speeds_source(folder: Folder, rows: Sequence[Sequence[Any]] = ()) -> dict[str, Any]:
    """Pump speeds, a state series whose property has bounds in the ontology."""
    folder.write("speeds", SPEEDS, rows)
    return csv_source(
        folder,
        {"speeds": {"pump": "string", "ts": TIME, "speed": "double"}},
        {
            "speeds": {
                "dataset": "speeds",
                "object_type": "Pump",
                "identity": ["pump"],
                "valid_time": "ts",
                "properties": columns("speed"),
            }
        },
    )


def readings_source(
    folder: Folder, rows: Sequence[Sequence[Any]] = (), cursor: str | None = None
) -> dict[str, Any]:
    """Sensor readings as events: one object each, found again by its identity."""
    folder.write("readings", READINGS, rows)
    mapping: dict[str, Any] = {
        "dataset": "readings",
        "object_type": "Reading",
        "identity": ["reading"],
        "valid_time": "ts",
        "properties": columns("sensor", "value", observed_at="ts", sequence="rev"),
    }
    if cursor is not None:
        mapping["cursor"] = cursor
    return csv_source(
        folder,
        {
            "readings": {
                "reading": "string",
                "sensor": "string",
                "ts": TIME,
                "value": "double",
                "rev": "int64",
            }
        },
        {"readings": mapping},
    )


# -- telemetry in long form, mapped through views ---------------------------------------

SIGNALS = ("signal", "asset", "quantity")
TELEMETRY = ("signal", "ts", "value")
# Which instrument measures what: the register the views join the readings with.
REGISTER = [
    ("L1", "T1", "level"),
    ("L2", "T2", "level"),
    ("P1", "J1", "pressure"),
    ("D1", "J1", "demand"),
    ("P2", "J2", "pressure"),
    ("D2", "J2", "demand"),
]
# A join with a register that is read whole.
TANK_LEVELS = (
    'SELECT s.asset AS tank, r.ts, r.value AS level FROM "telemetry"."readings" r '
    'JOIN "registry"."signals" s ON s.signal = r.signal WHERE s.quantity = \'level\''
)
# Several quantities of one time turned into one row.
JUNCTION_STATE = (
    "SELECT s.asset AS junction, r.ts, "
    "max(r.value) FILTER (WHERE s.quantity = 'pressure') AS pressure, "
    "max(r.value) FILTER (WHERE s.quantity = 'demand') AS demand "
    'FROM "telemetry"."readings" r JOIN "registry"."signals" s ON s.signal = r.signal '
    "WHERE s.quantity IN ('pressure', 'demand') GROUP BY s.asset, r.ts"
)
TANK_LEVEL_MAPPING = {
    "dataset": "tank_levels",
    "object_type": "Tank",
    "identity": ["tank"],
    "valid_time": "ts",
    "properties": columns("level"),
}
JUNCTION_STATE_MAPPING = {
    "dataset": "junction_state",
    "object_type": "Junction",
    "identity": ["junction"],
    "valid_time": "ts",
    "properties": columns("pressure", "demand"),
}


def register_signals(
    registry: dict[str, Any], folder: Folder, rows: Sequence[Sequence[Any]] = REGISTER
) -> dict[str, Any]:
    """Adds the register of instruments to a registry source; no mapping reads it itself."""
    folder.write("signals", SIGNALS, rows)
    registry["options"]["datasets"]["signals"] = {
        "glob": "signals.csv",
        "format": "csv",
        "types": {"signal": "string", "asset": "string", "quantity": "string"},
    }
    return registry


def telemetry_source(
    folder: Folder,
    rows: Sequence[Sequence[Any]] = (),
    *,
    declared: bool,
    mappings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Readings of every instrument in one dataset, mapped through two views over it.

    `declared` has the manifest say that the dataset is read on by its time.
    """
    folder.write("readings", TELEMETRY, rows)
    document = csv_source(
        folder,
        {"readings": {"signal": "string", "ts": TIME, "value": "double"}},
        mappings or {"levels": TANK_LEVEL_MAPPING, "junctions": JUNCTION_STATE_MAPPING},
        views={"tank_levels": TANK_LEVELS, "junction_state": JUNCTION_STATE},
    )
    if declared:
        document["incremental"] = {"readings": "ts"}
    return document


def asked_of(connect: Connecting, dataset: str) -> list[Any]:
    """The `since` of every read of a dataset, in the order they were made."""
    return [read["since"] for read in connect.asked if read["dataset"] == dataset]


# -- what a twin holds ---------------------------------------------------------------


def domain_changes(store: Store, before: Any, after: Any = "head") -> set[str]:
    """Types of the domain with any difference between two versions."""
    return set(store.diff(before, after)) - META_TYPES


def stored(store: Store, type_name: str, uid: str) -> dict[str, Any]:
    """The record the twin keeps of a source, a dataset or a run."""
    rows = store.table(type_name).to_pylist()
    return next(row for row in rows if row["uid"] == uid)
