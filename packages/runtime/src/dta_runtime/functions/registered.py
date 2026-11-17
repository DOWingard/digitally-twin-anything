"""Running a function a twin brings as code: the worker's handler of `function` requests.

The worker calls `run` inside a fresh process with limits, after unpacking the
request's code artifact and putting it first on `sys.path`. The function is
called with a reader of the twin and the request's arguments, and what it gives
back is turned into the result the worker answers with:

- an `Answer`, or a bare JSON value or table (an answer without unit, interval
  or notes);
- `Derived` rows, stored as an Arrow file among the twin's artifacts and named
  by their hash, for the API to write.

Anything else, and any value that is not JSON, is refused with
`function.bad_result`. This module imports neither the store nor the graph.
"""

from __future__ import annotations

import importlib
import inspect
import math
from collections.abc import Callable, Mapping
from typing import Any

import pyarrow as pa
from dta_core.log import Log
from dta_core.schema import UID_FIELD, FactKind
from pydantic import ValidationError

from ..protocol import (
    Answer,
    Artifacts,
    Derived,
    FunctionRequest,
    FunctionResult,
    Interval,
    Reader,
)
from .errors import BAD_ENTRY, BAD_PARAM, BAD_RESULT, FunctionError, refused

log = Log("functions.registered")

ARROW_FILE = "application/vnd.apache.arrow.file"
# The worker measures the run; the result's field cannot be left out.
_NOT_MEASURED = 0.0


def _bad_result(request: FunctionRequest, message: str, **detail: Any) -> FunctionError:
    log.error(
        "registered function gave back something that cannot be used",
        entry=request.entry,
        reason=message,
        **detail,
    )
    return refused(BAD_RESULT, f"{request.entry}: {message}", function=request.entry, **detail)


def _load(request: FunctionRequest) -> Callable[..., Any]:
    module_name, _, name = request.entry.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        # Raised for the entry's own module, or by an import inside it: only the
        # first means the entry names nothing.
        if exc.name is None or not (module_name + ".").startswith(exc.name + "."):
            log.error("importing a registered function failed", exc=exc, entry=request.entry)
            raise
        log.error("the module of a registered function is not in its code", entry=request.entry)
        raise refused(
            BAD_ENTRY,
            f"{request.entry}: the code artifact has no module {module_name!r}",
            function=request.entry,
            artifact=request.artifact,
        ) from exc
    except Exception as exc:
        log.error("importing a registered function failed", exc=exc, entry=request.entry)
        raise
    function = getattr(module, name, None)
    if not callable(function):
        log.error("a registered function is not in its module", entry=request.entry)
        raise refused(
            BAD_ENTRY,
            f"{request.entry}: module {module_name!r} has no function {name!r}",
            function=request.entry,
            artifact=request.artifact,
        )
    return function


def _json(request: FunctionRequest, value: Any, where: str = "$") -> Any:
    """`value` as plain JSON, or a refusal naming where in it something is not."""
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _bad_result(request, f"{where} is {value!r}, which JSON cannot hold", where=where)
        return value
    if isinstance(value, list | tuple):
        return [_json(request, item, f"{where}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        found = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise _bad_result(
                    request,
                    f"{where} has the key {key!r}; the keys of a JSON object are text",
                    where=where,
                )
            found[key] = _json(request, item, f"{where}.{key}")
        return found
    raise _bad_result(
        request,
        f"{where} is a {type(value).__name__}, which is not JSON; give a number, text, "
        "true or false, null, a list or a mapping (a time as ISO 8601 text)",
        where=where,
        type=type(value).__name__,
    )


def _result(request: FunctionRequest, **fields: Any) -> FunctionResult:
    try:
        return FunctionResult(duration_ms=_NOT_MEASURED, **fields)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise _bad_result(request, f"{where}: {first['msg']}", where=where) from exc


def _derived(request: FunctionRequest, found: Derived, artifacts: Artifacts) -> FunctionResult:
    rows = found.rows
    if not isinstance(rows, pa.Table) or UID_FIELD not in rows.column_names:
        raise _bad_result(
            request, "the rows of a Derived are a table with a `uid` column", where="rows"
        )
    try:
        kind = FactKind(found.kind)
    except ValueError as exc:
        raise _bad_result(request, f"{found.kind!r} is not a kind of fact", where="kind") from exc
    if kind == FactKind.observed:
        raise _bad_result(
            request,
            "a function works its rows out; it cannot write them as observed",
            where="kind",
        )
    confidence = found.confidence
    if confidence is not None and (
        isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not 0.0 <= confidence <= 1.0
    ):
        raise _bad_result(
            request, f"confidence is a number from 0 to 1, not {confidence!r}", where="confidence"
        )
    sink = pa.BufferOutputStream()
    with pa.ipc.new_file(sink, rows.schema) as writer:
        writer.write_table(rows)
    content = sink.getvalue().to_pybytes()
    sha256 = artifacts.put(content, ARROW_FILE)
    log.info(
        "derived rows stored",
        entry=request.entry,
        type=found.type,
        rows=rows.num_rows,
        bytes=len(content),
        sha256=sha256,
    )
    return _result(
        request,
        derived=sha256,
        derived_type=found.type,
        derived_kind=kind.value,
        derived_confidence=None if confidence is None else float(confidence),
    )


def run(
    request: FunctionRequest, twin: Reader, artifacts: Artifacts
) -> FunctionResult | tuple[pa.Table, FunctionResult]:
    """Call the function `request.entry` names with the reader and the request's
    arguments; its result, with the table when it gave one. `duration_ms` and `logs`
    are left for the worker to fill."""
    function = _load(request)
    try:
        inspect.signature(function).bind(twin, **request.args)
    except TypeError as exc:
        log.warn(
            "arguments do not fit a registered function",
            entry=request.entry,
            arguments=sorted(request.args),
            reason=str(exc),
        )
        raise refused(
            BAD_PARAM,
            f"{request.entry} cannot be called with {sorted(request.args)}: {exc}",
            function=request.entry,
        ) from exc
    except ValueError:
        # A callable without a signature to inspect is called as it is.
        log.debug("a registered function has no signature to check", entry=request.entry)
    try:
        returned = function(twin, **request.args)
    except Exception as exc:
        log.error(
            "registered function raised",
            exc=exc,
            entry=request.entry,
            arguments=sorted(request.args),
            at=request.at,
            branch=request.branch,
        )
        raise
    if isinstance(returned, Derived):
        return _derived(request, returned, artifacts)
    answer = returned if isinstance(returned, Answer) else Answer(value=returned)
    interval = answer.interval
    if interval is not None and not isinstance(interval, Interval):
        raise _bad_result(
            request,
            f"the interval of an answer is an Interval, not a {type(interval).__name__}",
            where="interval",
        )
    shared = {
        "unit": answer.unit,
        "interval": None
        if interval is None
        else _json(
            request,
            {"low": interval.low, "high": interval.high, "level": interval.level},
            "interval",
        ),
        "notes": answer.notes,
    }
    if isinstance(answer.value, pa.Table):
        result = _result(request, table=True, **shared)
        log.debug(
            "registered function gave a table", entry=request.entry, rows=answer.value.num_rows
        )
        return answer.value, result
    result = _result(request, value=_json(request, answer.value), **shared)
    log.debug("registered function gave a value", entry=request.entry)
    return result
