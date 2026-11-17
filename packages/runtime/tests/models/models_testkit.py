"""What the model tests hand a handler in place of the worker's own: a reader whose
frames the test built, and artifacts kept in a dict."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pyarrow as pa
from dta_runtime.protocol import (
    BacktestRequest,
    FitRequest,
    FrameRef,
    PredictRequest,
)

T0 = datetime(2026, 1, 5, tzinfo=UTC)
STEP = timedelta(minutes=15)
TIME = pa.timestamp("us", tz="UTC")
RUN = {"budget_s": 240.0, "trace_id": "0af7651916cd43dd8448eb211c80319c"}
TARGET = "Tank.level"


def at(steps: int) -> datetime:
    return T0 + steps * STEP


def frame(
    series: Mapping[str, Sequence[float | None]],
    *,
    features: Mapping[str, Mapping[str, Sequence[float | None]]] | None = None,
    first: int = 0,
    step: timedelta = STEP,
    starts: Mapping[str, int] | None = None,
) -> pa.Table:
    """A frame as the twin serves one: a row per series and grid time, sorted. A series
    starts at `first` steps after T0 unless `starts` says otherwise."""
    uids, times, values = [], [], []
    extra: dict[str, list[float | None]] = {name: [] for name in features or {}}
    for uid in sorted(series):
        begin = (starts or {}).get(uid, first)
        for index, value in enumerate(series[uid]):
            uids.append(uid)
            times.append(T0 + (begin + index) * step)
            values.append(None if value is None else float(value))
            for name, columns in (features or {}).items():
                found = columns[uid][index]
                extra[name].append(None if found is None else float(found))
    return pa.table(
        {
            "unique_id": pa.array(uids, pa.string()),
            "ds": pa.array(times, TIME),
            "y": pa.array(values, pa.float64()),
            **{name: pa.array(column, pa.float64()) for name, column in extra.items()},
        }
    )


class Frames:
    """A reader of the twin that serves the frames a test built, by the `start` of the
    reference that names them."""

    at = "v9"
    branch = "main"

    def __init__(self, **frames: pa.Table) -> None:
        self.frames = dict(frames)
        self.asked: list[FrameRef] = []

    def get(self, path: str, **params: Any) -> Any:
        raise AssertionError(f"a handler read {path}")

    def table(self, path: str, **params: Any) -> pa.Table:
        raise AssertionError(f"a handler read {path}")

    def frame(self, ref: FrameRef) -> pa.Table:
        self.asked.append(ref)
        return self.frames[ref.start]


class Kept:
    """Artifacts in a dict, by the SHA-256 of their content."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.media: dict[str, str] = {}

    def get(self, sha256: str) -> bytes:
        return self.files[sha256]

    def put(self, content: bytes, media_type: str) -> str:
        digest = hashlib.sha256(content).hexdigest()
        self.files[digest] = content
        self.media[digest] = media_type
        return digest


def ref(name: str, *, step: timedelta = STEP, **more: Any) -> dict[str, Any]:
    return {"target": TARGET, "start": name, "end": "end", "branch": "main", "step": step, **more}


def family_ref(family: str | Mapping[str, Any]) -> dict[str, Any]:
    return {"name": family} if isinstance(family, str) else dict(family)


def fit_request(family: str | Mapping[str, Any], horizon: int = 8, **more: Any) -> FitRequest:
    step = more.pop("step", STEP)
    return FitRequest(
        **RUN, family=family_ref(family), frame=ref("fit", step=step), horizon=horizon, **more
    )


def backtest_request(family: str | Mapping[str, Any], **more: Any) -> BacktestRequest:
    step = more.pop("grid", STEP)
    body = {
        **RUN,
        "family": family_ref(family),
        "frame": ref("fit", step=step),
        "horizon": 8,
        "step": 4,
        "windows": 5,
        "metrics": ["mae"],
        **more,
    }
    return BacktestRequest(**body)


def predict_request(
    family: str | Mapping[str, Any], state: str, horizon: int = 8, **more: Any
) -> PredictRequest:
    step = more.pop("step", STEP)
    drivers = more.pop("drivers", False)
    return PredictRequest(
        **RUN,
        family=family_ref(family),
        state=state,
        history=ref("history", step=step),
        horizon=horizon,
        drivers=ref("drivers", step=step) if drivers else None,
        **more,
    )


def season(length: int, period: int, *, phase: float = 0.0, level: float = 10.0) -> np.ndarray:
    """A pure season: the same `period` values over and over."""
    return level + 3.0 * np.sin(2 * np.pi * (np.arange(length) + phase) / period)


def noisy(length: int, period: int, seed: int, *, trend: float = 0.01) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (
        season(length, period, phase=seed) + trend * np.arange(length) + rng.normal(0, 0.3, length)
    )
