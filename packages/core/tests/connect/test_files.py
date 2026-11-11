"""The file connector on a local directory and on a real HTTP server serving the
same directory through its HTML index."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from dta_core.connect import open_connector
from dta_core.manifest.models import Source
from dta_core.sources import (
    BATCH_ROWS,
    Connector,
    SourceConfigError,
    SourceReadError,
    SourceUnavailable,
    UnknownDataset,
)

UTC_US = pa.timestamp("us", tz="UTC")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _telemetry(root: Path) -> None:
    """Three dated CSV files of zone-less timestamps, written out of name order."""
    for day in (3, 1, 2):
        lines = ["ts,sensor,value,zip"]
        for minute in range(4):
            lines.append(
                f"2024-01-0{day}T00:0{minute}:00,s{minute % 2},{day * 10 + minute},0012{day}"
            )
        _write(root / "telemetry" / f"2024-01-0{day}.csv", "\n".join(lines) + "\n")


def _parquet(root: Path, rows: int = 100) -> None:
    start = datetime(2024, 1, 1)
    table = pa.table(
        {
            "id": pa.array(range(rows), pa.int64()),
            "ts": pa.array(
                [start + timedelta(seconds=10 * i) for i in range(rows)], pa.timestamp("ns")
            ),
            "value": pa.array([i * 0.5 for i in range(rows)]),
            "label": pa.array([f"l{i % 3}" for i in range(rows)]).dictionary_encode(),
        }
    )
    (root / "parquet").mkdir(parents=True, exist_ok=True)
    pq.write_table(table, root / "parquet" / "part-0.parquet", row_group_size=25)


def _events(root: Path) -> None:
    records = [
        {"id": 1, "at": "2024-01-01T00:00:00Z", "kind": "start"},
        {"id": 2, "at": "2024-01-01T03:00:00+02:00", "kind": "stop"},
        {"id": 3, "at": "2024-01-01T02:30:00", "kind": "note"},
    ]
    _write(root / "events" / "events.jsonl", "".join(json.dumps(r) + "\n" for r in records))


DATASETS: dict[str, Any] = {
    "telemetry": {"glob": "telemetry/*.csv", "format": "csv", "timezone": "Europe/Berlin"},
    "readings": {"glob": "parquet/*.parquet", "format": "parquet"},
    "events": {"glob": "events/*.jsonl", "format": "jsonl", "timezone": "Europe/Berlin"},
}


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    _telemetry(root)
    _parquet(root)
    _events(root)
    return root


@pytest.fixture(params=["local", "http"])
def open_folder(
    request: pytest.FixtureRequest, http_server: Callable[[Path], str]
) -> Callable[..., Connector]:
    """Open a folder as a file source, either directly or through HTTP."""

    def make(root: Path, datasets: dict[str, Any] | None = None) -> Connector:
        if request.param == "local":
            location, env = "file://${DTA_TEST_DATA_DIR}", {"DTA_TEST_DATA_DIR": str(root)}
        else:
            location, env = "${DTA_TEST_DATA_URL}/", {"DTA_TEST_DATA_URL": http_server(root)}
        source = Source(
            connector="file", location=location, options={"datasets": datasets or DATASETS}
        )
        return open_connector("files", source, env)

    make.kind = request.param  # type: ignore[attr-defined]
    return make


def _read_all(connector: Connector, dataset: str, **kwargs: Any) -> pa.Table:
    tables = list(connector.read(dataset, **kwargs))
    assert tables and len({t.schema for t in tables}) == 1
    assert all(t.num_rows <= BATCH_ROWS for t in tables)
    return pa.concat_tables(tables)


def test_glob_over_dated_csv_files_reads_them_in_name_order(data_dir: Path, open_folder) -> None:
    with open_folder(data_dir) as connector:
        assert connector.datasets() == ["events", "readings", "telemetry"]
        assert connector.files("telemetry") == [
            "telemetry/2024-01-01.csv",
            "telemetry/2024-01-02.csv",
            "telemetry/2024-01-03.csv",
        ]
        table = _read_all(connector, "telemetry")
    assert table.num_rows == 12
    assert table.schema.field("ts").type == UTC_US
    # Zone-less text is wall-clock time in Europe/Berlin (UTC+1 in January).
    assert table.column("ts")[0].as_py() == datetime(2023, 12, 31, 23, tzinfo=UTC)
    assert table.column("value").to_pylist()[:5] == [10, 11, 12, 13, 20]


def test_csv_columns_limit_and_since(data_dir: Path, open_folder) -> None:
    with open_folder(data_dir) as connector:
        table = _read_all(
            connector,
            "telemetry",
            columns=["value"],
            cursor_column="ts",
            since=datetime(2024, 1, 1, 23, 1, tzinfo=UTC),
            limit=3,
        )
    assert table.schema.names == ["value"]
    assert table.column("value").to_pylist() == [22, 23, 30]


def test_types_override_inference(data_dir: Path, open_folder) -> None:
    datasets = {
        "telemetry": {
            "glob": "telemetry/*.csv",
            "format": "csv",
            "types": {"zip": "string", "value": "float64", "ts": "string"},
        }
    }
    with open_folder(data_dir, datasets) as connector:
        table = _read_all(connector, "telemetry", limit=1)
    assert table.schema.field("zip").type == pa.string()
    assert table.column("zip")[0].as_py() == "00121"
    assert table.schema.field("value").type == pa.float64()
    # A column declared as text is left as text even when it looks like timestamps.
    assert table.schema.field("ts").type == pa.string()


def test_iso_timestamps_with_mixed_zones_in_csv(tmp_path: Path, open_folder) -> None:
    _write(
        tmp_path / "mixed.csv",
        "at,n\n2024-01-01T00:00:00+01:00,1\n2024-01-01T00:00:00,2\n,3\n2024-01-01 01:00:00Z,4\n",
    )
    datasets = {"m": {"glob": "*.csv", "format": "csv", "timezone": "America/New_York"}}
    with open_folder(tmp_path, datasets) as connector:
        table = _read_all(connector, "m")
    assert table.schema.field("at").type == UTC_US
    assert table.column("at").to_pylist() == [
        datetime(2023, 12, 31, 23, tzinfo=UTC),
        datetime(2024, 1, 1, 5, tzinfo=UTC),
        None,
        datetime(2024, 1, 1, 1, tzinfo=UTC),
    ]


def test_parquet_columns_since_and_limit_are_pushed_into_the_reader(
    data_dir: Path, open_folder
) -> None:
    since = datetime(2024, 1, 1, 0, 15, tzinfo=UTC)  # row 90 is at 00:15:00
    with open_folder(data_dir) as connector:
        table = _read_all(
            connector, "readings", columns=["id", "label"], cursor_column="ts", since=since
        )
        assert table.schema.names == ["id", "label"]
        assert table.schema.field("label").type == pa.string()
        assert table.column("id").to_pylist() == list(range(91, 100))
        limited = _read_all(connector, "readings", cursor_column="id", since=10, limit=4)
        assert limited.column("id").to_pylist() == [11, 12, 13, 14]
        assert limited.schema.field("ts").type == UTC_US
        empty = list(connector.read("readings", cursor_column="id", since=1000))
        assert len(empty) == 1 and empty[0].num_rows == 0
        assert empty[0].schema.names == ["id", "ts", "value", "label"]


def test_jsonl_timestamps_keep_their_zones(data_dir: Path, open_folder) -> None:
    with open_folder(data_dir) as connector:
        table = _read_all(connector, "events")
    assert table.schema.field("at").type == UTC_US
    assert table.column("at").to_pylist() == [
        datetime(2024, 1, 1, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, 1, 30, tzinfo=UTC),  # zone-less, read in Europe/Berlin
    ]


def test_files_with_different_columns_are_a_read_error_naming_the_file(
    data_dir: Path, open_folder
) -> None:
    _write(
        data_dir / "telemetry" / "2024-01-04.csv",
        "ts,sensor,value,zip,extra\n2024-01-04T00:00:00,s0,1,1,x\n",
    )
    _write(
        data_dir / "telemetry" / "2024-01-05.csv",
        "ts,sensor,value,zip\n2024-01-05T00:00:00,s0,abc,1\n",
    )
    with open_folder(data_dir) as connector:
        with pytest.raises(SourceReadError) as caught:
            list(connector.read("telemetry"))
        assert caught.value.detail["file"] == "telemetry/2024-01-04.csv"
        assert caught.value.detail["columns"] == ["extra"]
        assert "telemetry/2024-01-04.csv" in caught.value.message
        # Reading only shared columns passes the extra file; the next one has text in `value`.
        with pytest.raises(SourceReadError) as caught:
            list(connector.read("telemetry", columns=["ts", "value"]))
        assert caught.value.detail["file"] == "telemetry/2024-01-05.csv"
        assert caught.value.detail["columns"] == ["value"]


def test_a_column_missing_from_the_first_file_is_a_config_error(
    data_dir: Path, open_folder
) -> None:
    with open_folder(data_dir) as connector:
        with pytest.raises(SourceConfigError) as caught:
            list(connector.read("telemetry", columns=["nope"]))
        assert caught.value.detail["missing_columns"] == ["nope"]
        with pytest.raises(SourceConfigError):
            list(connector.read("readings", columns=["id"], cursor_column="nope", since=1))


def test_a_glob_that_matches_no_file_is_a_read_error(data_dir: Path, open_folder) -> None:
    datasets = {"gone": {"glob": "missing/*.csv", "format": "csv"}}
    with open_folder(data_dir, datasets) as connector:
        with pytest.raises(SourceReadError) as caught:
            list(connector.read("gone"))
        assert caught.value.detail["glob"] == "missing/*.csv"
        with pytest.raises(UnknownDataset):
            connector.read("telemetry")


def test_large_parquet_comes_in_batches(tmp_path: Path, open_folder) -> None:
    _parquet(tmp_path, rows=BATCH_ROWS + 10)
    with open_folder(tmp_path, {"readings": DATASETS["readings"]}) as connector:
        sizes = [t.num_rows for t in connector.read("readings", columns=["id"])]
    assert sum(sizes) == BATCH_ROWS + 10 and max(sizes) <= BATCH_ROWS


def test_missing_location_is_unavailable(tmp_path: Path) -> None:
    source = Source(
        connector="file",
        location="file://${DTA_TEST_DATA_DIR}/absent",
        options={"datasets": DATASETS},
    )
    with pytest.raises(SourceUnavailable):
        open_connector("files", source, {"DTA_TEST_DATA_DIR": str(tmp_path)})


def test_unreachable_http_server_is_unavailable() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    source = Source(
        connector="file",
        location="http://127.0.0.1:${DTA_TEST_PORT}/",
        options={"datasets": DATASETS},
    )
    with pytest.raises(SourceUnavailable):
        open_connector("files", source, {"DTA_TEST_PORT": str(port)})


@pytest.mark.parametrize(
    ("location", "driver"),
    [("s3://bucket/data", "s3fs"), ("gcs://bucket/data", "gcsfs"), ("abfs://c/data", "adlfs")],
)
def test_a_scheme_without_its_driver_names_the_driver(location: str, driver: str) -> None:
    source = Source(connector="file", location=location, options={"datasets": DATASETS})
    with pytest.raises(SourceConfigError) as caught:
        open_connector("files", source, {})
    assert caught.value.detail["driver"] == driver
    assert driver in caught.value.message


def test_an_unknown_scheme_is_a_config_error() -> None:
    source = Source(connector="file", location="nosuch://x", options={"datasets": DATASETS})
    with pytest.raises(SourceConfigError):
        open_connector("files", source, {})
