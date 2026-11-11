"""The profiler on synthetic series whose answers are known, read through real
Parquet files. Tolerances are fixed here, before any measurement."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from dta_connect_testkit import SqliteBackend
from dta_core.connect import open_connector, profile_source
from dta_core.connect.profile import grouped_median
from dta_core.errors import ConfigError
from dta_core.manifest.models import Source, SourceProfile
from dta_core.sources import Connector

SIGMA_TOLERANCE_TREND = 0.15
SIGMA_TOLERANCE_SINE = 0.20
TAU_TOLERANCE = 0.30

START = datetime(2024, 1, 1, tzinfo=UTC)
STEP = timedelta(seconds=10)
PROFILED_AT = datetime(2026, 11, 11, 12, tzinfo=UTC)


def _times(count: int, step: timedelta = STEP) -> pa.Array:
    micros = np.arange(count, dtype=np.int64) * int(step / timedelta(microseconds=1))
    base = int(START.timestamp() * 1_000_000)
    return pa.array(base + micros, pa.timestamp("us", tz="UTC"))


def _source(tmp_path: Path, tables: dict[str, pa.Table]) -> Connector:
    datasets = {}
    for name, table in tables.items():
        folder = tmp_path / name
        folder.mkdir(parents=True)
        pq.write_table(table, folder / "data.parquet")
        datasets[name] = {"glob": f"{name}/*.parquet", "format": "parquet"}
    source = Source(
        connector="file",
        location="file://${DTA_TEST_PROFILE_DIR}",
        options={"datasets": datasets},
    )
    return open_connector("profiled", source, {"DTA_TEST_PROFILE_DIR": str(tmp_path)})


def _profile(tmp_path: Path, tables: dict[str, pa.Table], **kwargs: object) -> SourceProfile:
    with _source(tmp_path, tables) as connector:
        return profile_source(connector, profiled_at=PROFILED_AT, **kwargs)


def _ar1(rng: np.random.Generator, phi: float, entities: int, points: int) -> np.ndarray:
    """AR(1) series x_t = phi x_(t-1) + e_t, started from the stationary distribution."""
    noise = rng.normal(size=(entities, points))
    series = np.empty_like(noise)
    series[:, 0] = noise[:, 0] / math.sqrt(1 - phi**2)
    for t in range(1, points):
        series[:, t] = phi * series[:, t - 1] + noise[:, t]
    return series


def test_noise_floor_of_white_noise_on_a_linear_trend(tmp_path: Path) -> None:
    sigma = 0.5
    rng = np.random.default_rng(7)
    n = 5_000
    x = 3.0 + 0.01 * np.arange(n) + rng.normal(0.0, sigma, n)
    profile = _profile(tmp_path, {"trend": pa.table({"ts": _times(n), "x": x})})
    dataset = profile.datasets["trend"]
    estimate = dataset.columns["x"].noise_sigma
    assert estimate is not None
    assert abs(estimate - sigma) <= SIGMA_TOLERANCE_TREND * sigma, estimate
    assert dataset.time_column == "ts"
    assert dataset.entity_columns == []
    assert dataset.refresh_period == STEP
    assert profile.refresh_period == STEP


def test_a_slow_sine_is_not_counted_as_noise(tmp_path: Path) -> None:
    sigma = 0.3
    rng = np.random.default_rng(11)
    n = 10_000
    x = 10.0 * np.sin(2 * math.pi * np.arange(n) / 2_000) + rng.normal(0.0, sigma, n)
    profile = _profile(tmp_path, {"sine": pa.table({"ts": _times(n), "x": x})})
    estimate = profile.datasets["sine"].columns["x"].noise_sigma
    assert estimate is not None
    assert abs(estimate - sigma) <= SIGMA_TOLERANCE_SINE * sigma, estimate


@pytest.mark.parametrize("phi", [0.8, 0.9, 0.95])
def test_change_time_of_an_ar1_process(tmp_path: Path, phi: float) -> None:
    entities, points = 20, 5_000
    rng = np.random.default_rng(int(phi * 100))
    series = _ar1(rng, phi, entities, points)
    table = pa.table(
        {
            "sensor": np.repeat([f"s{k:02d}" for k in range(entities)], points),
            "ts": pa.concat_arrays([_times(points)] * entities),
            "x": series.reshape(-1),
        }
    )
    # Rows of different entities are interleaved, as a real table would hold them.
    table = table.take(np.random.default_rng(1).permutation(table.num_rows))
    profile = _profile(tmp_path, {"ar": table})
    dataset = profile.datasets["ar"]
    assert dataset.entity_columns == ["sensor"]
    assert dataset.candidate_keys == [["sensor", "ts"]]
    expected = -STEP.total_seconds() / math.log(phi)
    estimate = dataset.columns["x"].change_time
    assert estimate is not None
    assert abs(estimate.total_seconds() - expected) <= TAU_TOLERANCE * expected, estimate


def test_change_time_is_absent_for_series_shorter_than_32_points(tmp_path: Path) -> None:
    rng = np.random.default_rng(3)
    entities, points = 10, 20
    table = pa.table(
        {
            "sensor": np.repeat([f"s{k}" for k in range(entities)], points),
            "ts": pa.concat_arrays([_times(points)] * entities),
            "x": rng.normal(size=entities * points),
        }
    )
    column = _profile(tmp_path, {"short": table}).datasets["short"].columns["x"]
    assert column.change_time is None
    assert column.noise_sigma is not None


def test_keys_and_entities_with_a_surrogate_id(tmp_path: Path) -> None:
    sensors, points = 4, 50
    rng = np.random.default_rng(5)
    table = pa.table(
        {
            "id": np.arange(sensors * points),
            "sensor": np.tile([f"s{k}" for k in range(sensors)], points),
            "ts": pa.array(
                np.repeat(_times(points).to_numpy(), sensors), pa.timestamp("us", tz="UTC")
            ),
            "value": rng.normal(size=sensors * points),
        }
    )
    dataset = _profile(tmp_path, {"r": table}).datasets["r"]
    # Floating point measurements are unique too, but they are not keys.
    assert dataset.candidate_keys == [["id"]]
    assert dataset.entity_columns == ["sensor"]
    assert dataset.columns["id"].noise_sigma is None
    assert dataset.columns["value"].noise_sigma is not None


def test_two_entity_columns_are_found_when_one_is_not_enough(tmp_path: Path) -> None:
    sites, sensors, points = 3, 2, 40
    count = sites * sensors * points
    table = pa.table(
        {
            "site": np.repeat([f"site{k}" for k in range(sites)], sensors * points),
            "sensor": np.tile(np.repeat(["a", "b"], points), sites),
            "ts": pa.concat_arrays([_times(points)] * (sites * sensors)),
            "flow": np.arange(count, dtype=np.float64),
        }
    )
    dataset = _profile(tmp_path, {"r": table}).datasets["r"]
    assert dataset.candidate_keys == []
    assert dataset.entity_columns == ["site", "sensor"]


def test_column_statistics(tmp_path: Path) -> None:
    n = 400
    values = np.arange(n, dtype=np.float64)
    table = pa.table(
        {
            "ts": _times(n),
            "level": pa.array([None if i % 4 == 0 else float(v) for i, v in enumerate(values)]),
            "count": pa.array(np.arange(n) % 7, pa.int64()),
            "state": pa.array([["on", "off", None][i % 3] for i in range(n)]),
            "flag": pa.array([i % 2 == 0 for i in range(n)]),
        }
    )
    dataset = _profile(tmp_path, {"s": table}).datasets["s"]
    level = dataset.columns["level"]
    assert level.null_fraction == pytest.approx(0.25)
    assert level.distinct_count == 300 and level.unique
    assert (level.minimum, level.maximum) == (1.0, 399.0)
    count = dataset.columns["count"]
    assert (count.distinct_count, count.unique, count.minimum, count.maximum) == (7, False, 0, 6)
    state = dataset.columns["state"]
    assert state.dtype == "string" and state.distinct_count == 2
    assert state.null_fraction == pytest.approx(133 / 400)
    assert state.minimum is None and state.maximum is None
    assert dataset.columns["ts"].minimum is None
    assert dataset.columns["ts"].dtype == "timestamp[us, tz=UTC]"
    assert dataset.timestamp_columns == ["ts"]
    assert dataset.history_start == START
    assert dataset.history_end == START + STEP * (n - 1)
    assert dataset.rows == n and dataset.sampled_rows is None
    assert dataset.candidate_keys == [["ts"]]


def test_time_column_is_the_timestamp_with_most_distinct_values(tmp_path: Path) -> None:
    n = 100
    table = pa.table(
        {
            "day": pa.array(np.repeat(_times(4, timedelta(days=1)).to_numpy(), 25)),
            "ts": _times(n),
            "x": np.arange(n, dtype=np.float64),
        }
    )
    dataset = _profile(tmp_path, {"t": table}).datasets["t"]
    assert dataset.timestamp_columns == ["day", "ts"]
    assert dataset.time_column == "ts"


def test_source_refresh_period_is_the_shortest_of_its_datasets(tmp_path: Path) -> None:
    fast = pa.table({"ts": _times(50, timedelta(seconds=5)), "x": np.arange(50.0)})
    slow = pa.table({"ts": _times(50, timedelta(minutes=1)), "x": np.arange(50.0)})
    static = pa.table({"name": ["a", "b"]})
    profile = _profile(tmp_path, {"fast": fast, "slow": slow, "static": static})
    assert profile.datasets["fast"].refresh_period == timedelta(seconds=5)
    assert profile.datasets["slow"].refresh_period == timedelta(minutes=1)
    assert profile.datasets["static"].refresh_period is None
    assert profile.datasets["static"].time_column is None
    assert profile.refresh_period == timedelta(seconds=5)
    assert profile.profiled_at == PROFILED_AT


def test_a_dataset_above_the_row_cap_is_counted_in_full_and_sampled(tmp_path: Path) -> None:
    n, cap = 1_000, 300
    table = pa.table({"id": np.arange(n), "ts": _times(n), "x": np.arange(n, dtype=np.float64)})
    dataset = _profile(tmp_path, {"big": table}, row_cap=cap).datasets["big"]
    assert dataset.rows == n
    assert dataset.sampled_rows == cap
    assert dataset.columns["id"].distinct_count == cap
    assert dataset.columns["id"].maximum == cap - 1
    assert dataset.history_start == START
    assert dataset.history_end == START + STEP * (n - 1)


def test_only_the_named_datasets_are_profiled(tmp_path: Path) -> None:
    tables = {"a": pa.table({"x": [1]}), "b": pa.table({"x": [2]})}
    profile = _profile(tmp_path, tables, datasets=["b"])
    assert list(profile.datasets) == ["b"]
    with pytest.raises(ConfigError):
        _profile(tmp_path / "again", tables, row_cap=0)


def test_an_empty_dataset_is_profiled(tmp_path: Path) -> None:
    table = pa.table(
        {"ts": pa.array([], pa.timestamp("us", tz="UTC")), "x": pa.array([], pa.float64())}
    )
    dataset = _profile(tmp_path, {"e": table}).datasets["e"]
    assert dataset.rows == 0 and dataset.time_column is None
    assert dataset.columns["x"].null_fraction == 0.0


def test_a_sql_source_is_profiled(sqlite: SqliteBackend) -> None:
    sqlite.execute(
        "CREATE TABLE r (id integer primary key, ts timestamp, amount numeric(8, 2))",
        "INSERT INTO r VALUES (1, '2024-01-01 00:00:00', 1.5), (2, '2024-01-01 00:01:00', 2.5)",
    )
    with sqlite.open() as connector:
        profile = profile_source(connector, profiled_at=PROFILED_AT)
    dataset = profile.datasets["r"]
    assert dataset.columns["amount"].dtype == "double"
    assert dataset.columns["amount"].maximum == 2.5
    assert dataset.time_column == "ts" and dataset.refresh_period == timedelta(minutes=1)


def test_grouped_median() -> None:
    values = np.array([5.0, 1.0, 3.0, 10.0, 20.0, 7.0])
    groups = np.array([0, 0, 0, 2, 2, 0])
    assert grouped_median(values, groups, 3).tolist()[0] == 4.0
    assert math.isnan(grouped_median(values, groups, 3)[1])
    assert grouped_median(values, groups, 3)[2] == 15.0
