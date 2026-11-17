"""Frames as the libraries want them: pandas, one complete and regular series per object.

A frame read from the twin has a row wherever the object existed and nulls
wherever nothing was stored. The libraries want every series to run from its
first value to the frame's last time without a hole. What does not is left
out here and counted, never filled in: a value the twin did not store is not
one a model should learn from.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd
import pyarrow as pa
from dta_core.log import Log

from ..protocol.families import DS, UNIQUE_ID, Y, prediction_columns
from .errors import bad_family, bad_frame

log = Log("models.series")

_KEYS = (UNIQUE_ID, DS, Y)
_TIME = "datetime64[ns, UTC]"
_ARROW_TIME = pa.timestamp("us", tz="UTC")
# How many series an error or a log line names.
NAMED = 10


@dataclass(frozen=True)
class Prepared:
    # The series kept, each complete from its first row to the frame's last time,
    # sorted by `unique_id`, `ds`.
    frame: pd.DataFrame
    # What was left out, as codes with counts.
    notes: tuple[str, ...]
    # The last time at which any series of the frame has a value.
    last: pd.Timestamp


def from_arrow(table: pa.Table, what: str = "frame") -> pd.DataFrame:
    """The frame as pandas, its times in the resolution the libraries are used with."""
    missing = [name for name in _KEYS if name not in table.column_names]
    if missing:
        raise bad_frame(f"the {what} has no column {', '.join(missing)}", missing=missing)
    frame = table.to_pandas()
    frame[UNIQUE_ID] = frame[UNIQUE_ID].astype(str)
    frame[DS] = pd.to_datetime(frame[DS], utc=True).astype(_TIME)
    return frame


def features_of(frame: pd.DataFrame) -> list[str]:
    return [name for name in frame.columns if name not in _KEYS]


def prepare(frame: pd.DataFrame, inputs: Sequence[str], step: timedelta) -> Prepared:
    """Drop the rows before each series' first complete one (the target and every input
    present); leave out a series with a hole after that, and one that stops before the
    frame's last time. Rows after the last value of the target are not history."""
    wanted = [Y, *inputs]
    frame = frame.sort_values([UNIQUE_ID, DS], kind="stable").reset_index(drop=True)
    valued = frame[Y].notna()
    if not valued.any():
        raise bad_frame("the frame holds no value of the target")
    last = frame.loc[valued, DS].max()
    every = frame[UNIQUE_ID].unique()
    complete = frame[wanted].notna().all(axis=1)
    frame = frame[complete.groupby(frame[UNIQUE_ID], sort=False).cummax()]
    ends = frame[DS].where(frame[Y].notna()).groupby(frame[UNIQUE_ID], sort=False).transform("max")
    frame = frame[frame[DS] <= ends]
    frame = frame.assign(_complete=frame[wanted].notna().all(axis=1))
    found = frame.groupby(UNIQUE_ID, sort=True).agg(
        first=(DS, "min"), end=(DS, "max"), rows=(DS, "size"), complete=("_complete", "all")
    )
    alive = found[found["end"] == last]
    span = alive["end"] - alive["first"]
    width = pd.Timedelta(step)
    regular = (span % width == pd.Timedelta(0)) & (alive["rows"] == span // width + 1)
    kept = alive.index[alive["complete"] & regular]
    gaps = alive.index.difference(kept)
    ended = pd.Index(every).difference(alive.index)
    notes = []
    for code, names in (("series_with_gaps", gaps), ("series_ended", ended)):
        if len(names):
            notes.append(f"{code}:{len(names)}")
            log.info("series left out", reason=code, count=len(names), series=list(names[:NAMED]))
    if not len(kept):
        raise bad_frame(
            f"no series is left: {len(gaps)} have a missing value or time after their first, "
            f"{len(ended)} end before the frame's last time",
            series_with_gaps=len(gaps),
            series_ended=len(ended),
        )
    frame = frame[frame[UNIQUE_ID].isin(kept)].drop(columns="_complete").reset_index(drop=True)
    return Prepared(frame, tuple(notes), last)


def require_rows(frame: pd.DataFrame, need: int, why: str) -> None:
    """Refuse a frame in which a series is shorter than the family needs."""
    sizes = frame.groupby(UNIQUE_ID, sort=True).size()
    short = sizes[sizes < need]
    if len(short):
        names = list(short.index[:NAMED])
        raise bad_frame(
            f"{len(short)} series have fewer than the {need} rows needed ({why}): "
            f"{', '.join(names)}{', ...' if len(short) > NAMED else ''}",
            series=names,
            need=need,
            shortest=int(short.min()),
        )


def value_ranges(frame: pd.DataFrame, columns: Sequence[str]) -> dict[str, tuple[float, float]]:
    """Per column, the lowest and the highest value in the frame."""
    ranges = {}
    for name in columns:
        values = frame[name].dropna()
        if len(values):
            ranges[name] = (float(values.min()), float(values.max()))
    return ranges


def outside(frame: pd.DataFrame, validity: Mapping[str, Sequence[float]]) -> list[str]:
    """The columns of the frame that hold a value beyond the range given for them."""
    found = []
    for name, (low, high) in validity.items():
        if name in frame.columns:
            values = frame[name].dropna()
            if len(values) and (values.min() < low or values.max() > high):
                found.append(name)
    return found


def prediction_table(prediction: pd.DataFrame, levels: Sequence[float]) -> pa.Table:
    """A prediction as the table the worker answers with: the columns in order, the
    rows by series and time."""
    columns = prediction_columns(levels)
    missing = [name for name in columns if name not in prediction.columns]
    if missing:
        raise bad_family(f"the prediction has no column {', '.join(missing)}", missing=missing)
    ordered = prediction[columns].sort_values([UNIQUE_ID, DS], kind="stable")
    arrays = [
        pa.array(ordered[UNIQUE_ID].astype(str), pa.string()),
        pa.array(pd.to_datetime(ordered[DS], utc=True)).cast(_ARROW_TIME),
        *(pa.array(ordered[name].astype(float), pa.float64()) for name in columns[2:]),
    ]
    return pa.Table.from_arrays(arrays, names=columns)
