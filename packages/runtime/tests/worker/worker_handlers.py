"""Handlers of the worker's tests, in place of the shipped ones.

What a shipped run does is told by its request's `params["do"]`; a run of code the
twin brought calls the entry the request names, from the unpacked archive.
"""

from __future__ import annotations

import importlib
import json
import os
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
from dta_core.errors import DtaError
from dta_core.log import Log
from dta_runtime.protocol import (
    Artifacts,
    FitRequest,
    FitResult,
    FunctionResult,
    PredictResult,
    Reader,
)

log = Log("plant.model")

T = datetime(2026, 1, 5, tzinfo=UTC)
NO_STATE = "0" * 64
HOLD_LIMIT_S = 60.0


def _entry(entry: str) -> Any:
    module, _, name = entry.partition(":")
    return getattr(importlib.import_module(module), name)


def _hold(params: dict[str, Any]) -> None:
    """Wait until the test lets the run go: it creates the release file."""
    Path(params["reached"]).touch()
    release = Path(params["release"])
    deadline = time.monotonic() + HOLD_LIMIT_S
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.01)


def _act(request: Any, twin: Reader, artifacts: Artifacts) -> tuple[pa.Table | None, list[str]]:
    """Do what the request asks; a table to return, if any, and notes."""
    params = request.params
    do = params.get("do", "noop")
    if do == "noop":
        return None, []
    if do == "pid":
        return None, [str(os.getpid())]
    if do == "log":
        for number in range(int(params.get("lines", 1))):
            log.info("fitting", line=number)
        log.debug("not kept: below info")
        return None, []
    if do == "table":
        table = pa.table({"unique_id": ["T1", "T1"], "mean": [1.5, 2.5]})
        return table.replace_schema_metadata({b"kept": b"yes"}), []
    if do == "read":
        health = twin.get("/meta/health")
        junctions = twin.table("/objects/Junction", limit=params.get("limit", 2))
        content = json.dumps({"at": twin.at, "rows": junctions.num_rows}).encode()
        stored = artifacts.put(content, "application/json")
        back = artifacts.get(stored)
        notes = [twin.at, twin.branch, json.dumps(health), stored, back.decode()]
        return junctions.select(["uid"]), notes
    if do == "frame":
        return twin.frame(request.history), [twin.at]
    if do == "sleep":
        time.sleep(float(params["s"]))
        return None, []
    if do == "allocate":
        hoard = bytearray(int(params["bytes"]))
        return None, [str(len(hoard))]
    if do == "raise":
        raise ValueError("the pump is on fire")
    if do == "bad_state":
        raise DtaError("the state was fitted to another frame", code="model.bad_state")
    if do == "refuse":
        raise DtaError("no such family here", code="model.unknown_family")
    if do == "hold":
        _hold(params)
        return None, []
    if do == "die":
        os.kill(os.getpid(), getattr(signal, params["signal"]))
        time.sleep(5)
    if do == "exit":
        raise SystemExit(3)
    raise ValueError(f"the test handlers do no {do!r}")


def predict(request: Any, twin: Reader, artifacts: Artifacts) -> Any:
    table, notes = _act(request, twin, artifacts)
    result = PredictResult(duration_ms=0, rows=0 if table is None else table.num_rows, series=1)
    result = result.model_copy(update={"notes": notes})
    return result if table is None else (table, result)


def fit(request: FitRequest, twin: Reader, artifacts: Artifacts) -> Any:
    if request.family.entry is not None:
        value = _entry(request.family.entry)(twin, artifacts, **request.params)
        notes = [json.dumps(value)]
    else:
        _table, notes = _act(request, twin, artifacts)
    return FitResult(
        duration_ms=0,
        state=NO_STATE,
        series=0,
        rows=0,
        fitted_from=T,
        fitted_to=T,
        notes=notes,
    )


def function(request: Any, twin: Reader, artifacts: Artifacts) -> Any:
    """A registered function: the entry is called with the reader, the artifacts and
    the request's arguments; a table it returns is the answer's table."""
    value = _entry(request.entry)(twin, artifacts, **request.args)
    if isinstance(value, pa.Table):
        return value, FunctionResult(duration_ms=0, table=True)
    return FunctionResult(duration_ms=0, value=value)


def not_a_result(request: Any, twin: Reader, artifacts: Artifacts) -> Any:
    return {"value": 1}
