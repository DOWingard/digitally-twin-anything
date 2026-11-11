"""Statistical profile of a source: what its datasets hold and how fast they change.

Everything is computed on whole columns with pyarrow.compute and numpy.
Per-entity statistics sort the rows by entity and time once and work on group
boundaries, so the cost does not grow with a Python loop over rows or entities.

A dataset larger than the row cap is still read to the end, to count its rows
and find its first and last timestamp; the column statistics, the noise floor
and the change time use the first `row_cap` rows.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from ..errors import ConfigError
from ..log import Log
from ..manifest.models import ColumnProfile, DatasetProfile, SourceProfile
from ..sources import Connector

log = Log("connect.profile")

ROW_CAP = 2_000_000
# Gaussian consistency factor: 1.4826 * MAD estimates a standard deviation.
MAD_TO_SIGMA = 1.4826
# The second difference of white noise of scale sigma has scale sqrt(6) * sigma.
SECOND_DIFFERENCE_SCALE = math.sqrt(6.0)
# Fewest second differences an entity needs before its noise floor counts.
MIN_SIGMA_DIFFERENCES = 3
# An autocorrelation estimated from fewer points is too noisy to time a decay.
MIN_CHANGE_POINTS = 32
DECAY_LEVEL = 1.0 / math.e
# Bounds the number of column pairs tried as keys: k columns make k(k-1)/2 pairs.
MAX_PAIR_COLUMNS = 24


@dataclass(frozen=True)
class _ColumnStats:
    index: int
    name: str
    type: pa.DataType
    nulls: int
    distinct: int
    unique: bool


def profile_source(
    connector: Connector,
    *,
    profiled_at: datetime,
    row_cap: int = ROW_CAP,
    datasets: Sequence[str] | None = None,
) -> SourceProfile:
    """Profile every dataset of the source, or only those named in `datasets`.

    `profiled_at` is recorded as given; the profiler never reads the clock.
    """
    if isinstance(row_cap, bool) or not isinstance(row_cap, int) or row_cap < 1:
        raise ConfigError("row_cap must be a positive integer", code="config.bad_value")
    started = time.perf_counter()
    names = list(datasets) if datasets is not None else connector.datasets()
    log.info("profile started", source=connector.source_id, datasets=names, row_cap=row_cap)
    profiles = {name: profile_dataset(connector, name, row_cap=row_cap) for name in names}
    periods = [p.refresh_period for p in profiles.values() if p.refresh_period is not None]
    result = SourceProfile(
        profiled_at=profiled_at,
        datasets=profiles,
        refresh_period=min(periods) if periods else None,
    )
    log.info(
        "profile finished",
        source=connector.source_id,
        datasets=len(profiles),
        refresh_period_s=result.refresh_period.total_seconds() if result.refresh_period else None,
        elapsed_ms=_elapsed_ms(started),
    )
    return result


def profile_dataset(
    connector: Connector, dataset: str, *, row_cap: int = ROW_CAP
) -> DatasetProfile:
    started = time.perf_counter()
    parts: list[pa.Table] = []
    kept = 0
    rows = 0
    bounds: dict[str, list[Any]] = {}
    for table in connector.read(dataset):
        rows += table.num_rows
        if kept < row_cap:
            part = table.slice(0, row_cap - kept)
            parts.append(part)
            kept += part.num_rows
        _track_bounds(table, bounds)
    sample = pa.concat_tables(parts).combine_chunks()
    read_ms = _elapsed_ms(started)

    stats = [_column_stats(i, f, sample.column(i)) for i, f in enumerate(sample.schema)]
    by_name = {s.name: s for s in stats}
    n = sample.num_rows
    keyable = [s for s in stats if _keyable(s.type) and s.nulls == 0 and n > 0]
    keys = _candidate_keys(sample, keyable, n)
    single_keys = {key[0] for key in keys if len(key) == 1}

    timestamp_columns = [s.name for s in stats if pa.types.is_timestamp(s.type)]
    time_column = _time_column([by_name[name] for name in timestamp_columns])
    entity_columns: list[str] = []
    numeric: list[str] = []
    noise: dict[str, float | None] = {}
    change: dict[str, timedelta | None] = {}
    refresh_period = None
    history_start = history_end = None
    if time_column is not None:
        history_start, history_end = bounds.get(time_column, [None, None])
        refresh_period = _refresh_period(sample.column(time_column))
        entity_columns = _entity_columns(sample, keyable, by_name[time_column], single_keys, n)
        numeric = [
            s.name
            for s in stats
            if (pa.types.is_integer(s.type) or pa.types.is_floating(s.type))
            and s.name not in entity_columns
            and s.name not in single_keys
        ]
        if numeric:
            noise, change = _series_statistics(sample, time_column, entity_columns, numeric)

    columns = {
        s.name: ColumnProfile(
            dtype=str(s.type),
            null_fraction=s.nulls / n if n else 0.0,
            distinct_count=s.distinct,
            unique=s.unique,
            minimum=_bound(sample.column(s.index), "min", s.type),
            maximum=_bound(sample.column(s.index), "max", s.type),
            noise_sigma=noise.get(s.name),
            change_time=change.get(s.name),
        )
        for s in stats
    }
    profile = DatasetProfile(
        rows=rows,
        columns=columns,
        candidate_keys=keys,
        timestamp_columns=timestamp_columns,
        time_column=time_column,
        entity_columns=entity_columns,
        history_start=history_start,
        history_end=history_end,
        refresh_period=refresh_period,
        sampled_rows=row_cap if rows > row_cap else None,
    )
    log.info(
        "dataset profiled",
        source=connector.source_id,
        dataset=dataset,
        rows=rows,
        sampled_rows=profile.sampled_rows,
        columns=len(columns),
        time_column=time_column,
        entity_columns=entity_columns,
        series_columns=numeric,
        read_ms=read_ms,
        elapsed_ms=_elapsed_ms(started),
    )
    return profile


# -- columns ------------------------------------------------------------------


def _track_bounds(table: pa.Table, bounds: dict[str, list[Any]]) -> None:
    for field in table.schema:
        if not pa.types.is_timestamp(field.type):
            continue
        extremes = pc.min_max(table.column(field.name))
        low, high = extremes["min"].as_py(), extremes["max"].as_py()
        if low is None:
            continue
        seen = bounds.setdefault(field.name, [low, high])
        seen[0] = min(seen[0], low)
        seen[1] = max(seen[1], high)


def _column_stats(index: int, field: pa.Field, column: pa.ChunkedArray) -> _ColumnStats:
    nulls = column.null_count
    try:
        distinct = pc.count_distinct(column, mode="only_valid").as_py()
    except (pa.ArrowNotImplementedError, pa.ArrowTypeError):
        log.debug(
            "distinct values not counted for this type", column=field.name, type=str(field.type)
        )
        return _ColumnStats(index, field.name, field.type, nulls, 0, False)
    valid = len(column) - nulls
    return _ColumnStats(
        index, field.name, field.type, nulls, distinct, valid > 0 and distinct == valid
    )


def _bound(column: pa.ChunkedArray, which: str, kind: pa.DataType) -> float | None:
    if not (pa.types.is_integer(kind) or pa.types.is_floating(kind)):
        return None
    value = pc.min_max(column)[which].as_py()
    if value is None or not math.isfinite(float(value)):
        return None
    return float(value)


def _keyable(kind: pa.DataType) -> bool:
    """Types whose values identify rows; floating point values are measurements, not keys."""
    return (
        pa.types.is_integer(kind)
        or pa.types.is_string(kind)
        or pa.types.is_large_string(kind)
        or pa.types.is_binary(kind)
        or pa.types.is_timestamp(kind)
        or pa.types.is_date(kind)
        or pa.types.is_time(kind)
        or pa.types.is_boolean(kind)
    )


def _codes(column: pa.ChunkedArray) -> tuple[np.ndarray, int]:
    """Dense integer codes of a column's values (null is a value of its own), and how many."""
    encoded = pc.dictionary_encode(column.combine_chunks(), null_encoding="encode")
    return encoded.indices.to_numpy(zero_copy_only=False).astype(np.int64), len(encoded.dictionary)


def _combined_codes(table: pa.Table, names: Sequence[str]) -> np.ndarray:
    """Codes of the combinations of several columns' values, kept below the row count."""
    combined, _ = _codes(table.column(names[0]))
    for name in names[1:]:
        codes, cardinality = _codes(table.column(name))
        # Re-encoding after each step keeps the product of cardinalities from overflowing.
        merged = pa.array(combined * cardinality + codes)
        combined, _ = _codes(pa.chunked_array([merged]))
    return combined


def _is_unique(table: pa.Table, names: Sequence[str]) -> bool:
    combined = _combined_codes(table, names)
    return pc.count_distinct(pa.array(combined)).as_py() == table.num_rows


def _candidate_keys(sample: pa.Table, keyable: list[_ColumnStats], n: int) -> list[list[str]]:
    singles = [[s.name] for s in keyable if s.unique]
    if singles or n == 0:
        return singles
    pool = sorted(keyable, key=lambda s: (-s.distinct, s.index))[:MAX_PAIR_COLUMNS]
    if len(keyable) > MAX_PAIR_COLUMNS:
        log.debug("key pairs tried among the most varied columns only", columns=len(keyable))
    pairs: list[tuple[_ColumnStats, _ColumnStats]] = []
    for i, first in enumerate(pool):
        for second in pool[i + 1 :]:
            # A pair can only be unique if it has at least as many combinations as rows.
            if first.distinct * second.distinct < n:
                continue
            if _is_unique(sample, [first.name, second.name]):
                pairs.append(tuple(sorted((first, second), key=lambda s: s.index)))
    pairs.sort(key=lambda pair: (pair[0].index, pair[1].index))
    return [[a.name, b.name] for a, b in pairs]


def _time_column(candidates: list[_ColumnStats]) -> str | None:
    usable = [s for s in candidates if s.distinct > 0]
    if not usable:
        return None
    return max(usable, key=lambda s: s.distinct).name


def _entity_columns(
    sample: pa.Table,
    keyable: list[_ColumnStats],
    time_stats: _ColumnStats,
    single_keys: set[str],
    n: int,
) -> list[str]:
    """The fewest columns that, with the time column, identify a row; none for one series.

    A single-column key such as a surrogate id would trivially qualify, so key
    columns are not considered; the coarsest qualifying column is preferred.
    """
    if n == 0 or (time_stats.unique and time_stats.nulls == 0):
        return []
    pool = sorted(
        (s for s in keyable if s.name != time_stats.name and s.name not in single_keys),
        key=lambda s: (s.distinct, s.index),
    )[:MAX_PAIR_COLUMNS]
    for column in pool:
        if column.distinct * time_stats.distinct >= n and _is_unique(
            sample, [column.name, time_stats.name]
        ):
            return [column.name]
    pairs = sorted(
        ((a, b) for i, a in enumerate(pool) for b in pool[i + 1 :]),
        key=lambda pair: (pair[0].distinct * pair[1].distinct, pair[0].index, pair[1].index),
    )
    for a, b in pairs:
        if a.distinct * b.distinct * time_stats.distinct < n:
            continue
        if _is_unique(sample, [a.name, b.name, time_stats.name]):
            return [s.name for s in sorted((a, b), key=lambda s: s.index)]
    return []


def _refresh_period(column: pa.ChunkedArray) -> timedelta | None:
    """Median gap between consecutive distinct timestamps."""
    distinct = pc.unique(column.drop_null())
    if len(distinct) < 2:
        return None
    instants = np.sort(pc.cast(distinct, pa.int64()).to_numpy(zero_copy_only=False))
    return _micros_to_timedelta(float(np.median(np.diff(instants))))


# -- series -------------------------------------------------------------------


def _series_statistics(
    sample: pa.Table, time_column: str, entity_columns: list[str], numeric: list[str]
) -> tuple[dict[str, float | None], dict[str, timedelta | None]]:
    started = time.perf_counter()
    series = sample.select([*entity_columns, time_column, *numeric])
    series = series.filter(pc.is_valid(series.column(time_column)))
    order = [(name, "ascending") for name in [*entity_columns, time_column]]
    series = series.sort_by(order).combine_chunks()
    n = series.num_rows
    if n == 0:
        return {}, {}
    if entity_columns:
        codes = _combined_codes(series, entity_columns)
        starts = np.empty(n, dtype=bool)
        starts[0] = True
        starts[1:] = codes[1:] != codes[:-1]
    else:
        starts = np.zeros(n, dtype=bool)
        starts[0] = True
    groups = np.cumsum(starts) - 1
    group_count = int(groups[-1]) + 1
    instants = pc.cast(series.column(time_column), pa.int64()).to_numpy(zero_copy_only=False)
    period = _sampling_period(instants, groups, group_count)

    noise: dict[str, float | None] = {}
    change: dict[str, timedelta | None] = {}
    for name in numeric:
        values = pc.cast(series.column(name), pa.float64()).to_numpy(zero_copy_only=False)
        noise[name] = noise_floor(values, groups, group_count)
        tau = change_time(values, groups, group_count, period)
        change[name] = _micros_to_timedelta(tau) if tau is not None else None
    log.debug(
        "series statistics computed",
        rows=n,
        entities=group_count,
        columns=len(numeric),
        elapsed_ms=_elapsed_ms(started),
    )
    return noise, change


def grouped_median(values: np.ndarray, groups: np.ndarray, group_count: int) -> np.ndarray:
    """Median of `values` per group id in `[0, group_count)`; NaN for an empty group."""
    order = np.lexsort((values, groups))
    ordered = values[order]
    counts = np.bincount(groups, minlength=group_count)
    starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
    medians = np.full(group_count, np.nan)
    filled = counts > 0
    low = starts[filled] + (counts[filled] - 1) // 2
    high = starts[filled] + counts[filled] // 2
    medians[filled] = (ordered[low] + ordered[high]) / 2.0
    return medians


def _sampling_period(instants: np.ndarray, groups: np.ndarray, group_count: int) -> np.ndarray:
    """Per entity, the median step between its consecutive distinct timestamps (microseconds)."""
    steps = np.diff(instants).astype(np.float64)
    usable = (groups[1:] == groups[:-1]) & (steps > 0)
    return grouped_median(steps[usable], groups[1:][usable], group_count)


def noise_floor(values: np.ndarray, groups: np.ndarray, group_count: int) -> float | None:
    """sigma = 1.4826 * MAD(second difference) / sqrt(6) per entity, median over entities.

    The second difference cancels a level and a linear trend, so slow real
    change is not counted as noise. `values` are ordered by time within each
    entity and entities are contiguous; missing values are skipped.
    """
    finite = np.isfinite(values)
    x = values[finite]
    g = groups[finite]
    if x.size < 3:
        return None
    within = g[2:] == g[:-2]
    second = (x[2:] - 2.0 * x[1:-1] + x[:-2])[within]
    owner = g[2:][within]
    if second.size == 0:
        return None
    counts = np.bincount(owner, minlength=group_count)
    centre = grouped_median(second, owner, group_count)
    spread = grouped_median(np.abs(second - centre[owner]), owner, group_count)
    sigma = MAD_TO_SIGMA * spread / SECOND_DIFFERENCE_SCALE
    sigma = sigma[(counts >= MIN_SIGMA_DIFFERENCES) & np.isfinite(sigma)]
    return float(np.median(sigma)) if sigma.size else None


def change_time(
    values: np.ndarray, groups: np.ndarray, group_count: int, period: np.ndarray
) -> float | None:
    """First lag where the autocorrelation drops below 1/e, times the sampling period.

    Per entity with at least 32 points, median over entities, in the unit of
    `period`. Autocorrelations of all entities of similar length come from one
    zero-padded FFT, so the work is a handful of array operations.
    """
    finite = np.isfinite(values)
    x = values[finite]
    g = groups[finite]
    counts = np.bincount(g, minlength=group_count)
    eligible = counts >= MIN_CHANGE_POINTS
    if not eligible.any():
        return None
    keep = eligible[g]
    x = x[keep]
    g = g[keep]
    entities, member = np.unique(g, return_inverse=True)
    lengths = np.bincount(member)
    starts = np.concatenate(([0], np.cumsum(lengths)[:-1]))
    position = np.arange(x.size) - starts[member]
    centred = x - (np.bincount(member, weights=x) / lengths)[member]

    lags = np.full(entities.size, np.nan)
    sizes = 1 << np.ceil(np.log2(lengths)).astype(np.int64)
    for size in np.unique(sizes):
        rows = np.nonzero(sizes == size)[0]
        row_of = np.full(entities.size, -1)
        row_of[rows] = np.arange(rows.size)
        chosen = row_of[member] >= 0
        block = np.zeros((rows.size, int(size)))
        block[row_of[member[chosen]], position[chosen]] = centred[chosen]
        # Zero padding to twice the length makes the circular correlation a linear one.
        spectrum = np.fft.rfft(block, n=2 * int(size), axis=1)
        covariance = np.fft.irfft(np.abs(spectrum) ** 2, n=2 * int(size), axis=1)[:, : int(size)]
        with np.errstate(divide="ignore", invalid="ignore"):
            correlation = covariance / covariance[:, :1]
        lag = np.arange(int(size))[None, :]
        below = (correlation < DECAY_LEVEL) & (lag >= 1) & (lag < lengths[rows][:, None])
        found = below.any(axis=1)
        lags[rows[found]] = below.argmax(axis=1)[found]
    tau = lags * period[entities]
    tau = tau[np.isfinite(tau) & (tau > 0)]
    return float(np.median(tau)) if tau.size else None


def _micros_to_timedelta(value: float) -> timedelta:
    return timedelta(microseconds=round(value))


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)
