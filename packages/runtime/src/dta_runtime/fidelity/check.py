"""A fidelity check: what is measured at a time of the twin's clock, what is decided
from it, and the one version that records both.

A check measures first, without holding anything: it reads what earlier checks kept,
asks the predictor for each question's origin, reads what was observed since, and runs
the tests of the observed signals. Only then, holding the package's write lock, it
reads the incidents, signals and state again, decides (plain functions of what was
read), and writes everything as one version at the check time. Its outcome is read
back from the records before and after that version, which is also how a check asked
for again at the same time answers without writing anything.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

import pyarrow as pa
from dta_core.errors import DtaError
from dta_core.log import Log, trace
from dta_core.schema import FactKind, Kind
from dta_core.store import Provenance, VersionRef, ViolationError
from dta_core.store.model import iso_utc

from .. import inforce
from ..protocol.families import DS, MEAN, UNIQUE_ID, high, low
from ..twin import Twin
from . import ladder, measures, quality, reading, records
from .errors import BAD_REQUEST, OUT_OF_ORDER, refused
from .ladder import FAILED, OPEN, REMODEL_REQUESTED, RESOLVED, UNRESOLVED
from .outcome import corrections_of, incident_events, state_event, unknown_events
from .reading import QuestionSetup, Scored, Setup
from .records import (
    BIASED,
    BREACH,
    CHECK_TYPE,
    FIDELITY_TYPE,
    INCIDENT_TYPE,
    SIGNAL,
    SIGNAL_TYPE,
    STALE,
    STATE_UID,
    STUCK,
    SUSPECT,
    WRITES,
    dumps,
    iso,
)
from .values import CheckOutcome, CorrectionRequest, Freshness, Predictor, correction_key

log = Log("runtime.fidelity")

TIME = pa.timestamp("us", tz="UTC")
LEVEL = 80
# A biased signal is cleared after this many consecutive sound checks, a reading within
# this many tolerances of its inferred value; a check with no inferred value to compare
# with neither counts nor resets.
CLEAR_CHECKS = 3
CLEAR_TOLERANCES = 2.0
# A stuck signal is cleared at the first check at which its raw readings changed in at
# least this many intervals since it was flagged, and the run without a change in
# progress is shorter than the shortest run the stuck test can call stuck.
STUCK_CLEAR_CHANGES = 3
# When nothing is declared, the range an inferred value may lie in is the range observed
# over the window, widened on each side by this share of its width and by no less than
# the inferring question's tolerance.
RANGE_MARGIN = 0.5
_TEXT = (pa.string(), pa.large_string())

# What becomes of a suspect signal's value at a check.
INFERRED, UNKNOWN, TURN_UNKNOWN = "inferred", "unknown", "turn_unknown"


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


def _reason(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


# -- what one question measured -------------------------------------------------------------


@dataclass
class Measured:
    """What a check measured of one question."""

    setup: QuestionSetup
    entry: dict[str, Any]
    # The origin this check scored, when it scored one.
    scored: Scored | None = None
    # Every scored origin known: earlier checks' and this one's.
    origins: list[Scored] = field(default_factory=list)
    # The last predicted value per series, and its time.
    estimates: dict[str, float] = field(default_factory=dict)
    estimates_at: datetime | None = None
    # The concrete type of each series, and its value in force at the check time.
    types: dict[str, str] = field(default_factory=dict)
    current: dict[str, float | None] = field(default_factory=dict)


class _Bad(Exception):
    pass


def _prediction(table: Any, grid: Sequence[datetime]) -> dict[str, dict[int, tuple]]:
    """Per series and grid time (microseconds): the mean and the 80 % interval's ends.
    Rows off the grid or without a finite mean are left out; anything else that cannot be
    read is refused."""
    if not isinstance(table, pa.Table):
        raise _Bad(f"the predictor gave a {type(table).__name__}, not a table")
    missing = [name for name in (UNIQUE_ID, DS, MEAN) if name not in table.column_names]
    if missing:
        raise _Bad(f"the prediction has no column {', '.join(missing)}")
    if table.schema.field(UNIQUE_ID).type not in _TEXT:
        raise _Bad(f"{UNIQUE_ID} is {table.schema.field(UNIQUE_ID).type}, not string")
    if not pa.types.is_timestamp(table.schema.field(DS).type):
        raise _Bad(f"{DS} is {table.schema.field(DS).type}, not a timestamp")
    try:
        means = table.column(MEAN).cast(pa.float64())
        bounds = [
            table.column(name).cast(pa.float64()) if name in table.column_names else None
            for name in (low(LEVEL), high(LEVEL))
        ]
        moments = table.column(DS).cast(TIME).cast(pa.int64())
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as exc:
        raise _Bad(f"the prediction's columns cannot be read as numbers and times: {exc}") from exc
    wanted = {reading.micros(moment) for moment in grid}
    found: dict[str, dict[int, tuple]] = {}
    lows = bounds[0].to_pylist() if bounds[0] is not None else [None] * table.num_rows
    highs = bounds[1].to_pylist() if bounds[1] is not None else [None] * table.num_rows
    for uid, moment, mean, lo, hi in zip(
        table.column(UNIQUE_ID).to_pylist(),
        moments.to_pylist(),
        means.to_pylist(),
        lows,
        highs,
        strict=True,
    ):
        if uid is None or moment not in wanted or mean is None or not math.isfinite(mean):
            continue
        series = found.setdefault(uid, {})
        if moment in series:
            raise _Bad(f"series {uid} is given twice at one time")
        series[moment] = (mean, lo, hi)
    if not found:
        raise _Bad("no row is at a time of the origin's grid with a finite mean")
    return found


def _spans(signals: Mapping[str, Mapping[str, Any]]) -> dict[tuple[str, str], list[tuple]]:
    """Per (Type.property, uid): every time it was suspect, as (since, until,
    unknown_since): until is None while it still is, unknown_since None while its value
    was inferred throughout."""
    spans: dict[tuple[str, str], list[tuple]] = {}
    for document in signals.values():
        key = (document["property"], document["object"])
        episodes = (document.get("detail") or {}).get("episodes") or []
        for episode in episodes:
            since = records.parse_time(episode.get("since"))
            if since is not None:
                spans.setdefault(key, []).append(
                    (
                        since,
                        records.parse_time(episode.get("until")),
                        records.parse_time(episode.get("unknown_since")),
                    )
                )
        if document.get("status") == SUSPECT and not episodes and document.get("since"):
            spans.setdefault(key, []).append((document["since"], None, None))
    return spans


def _suspect_in(spans: Sequence[tuple], start: datetime, end: datetime) -> bool:
    """Whether a signal was suspect at any time in (start, end]."""
    return any(since <= end and (until is None or until > start) for since, until, *_ in spans)


def _unknown_at(spans: Sequence[tuple], moment: datetime) -> bool:
    """Whether a signal's value in force at `moment` was unknown."""
    return any(
        unknown is not None and unknown <= moment and (until is None or until > moment)
        for _, until, unknown in spans
    )


def _concrete(setup: Setup, text: str) -> set[str]:
    """`Type.property` as the concrete types hold it: an abstract type stands for each."""
    type_name, _, name = str(text).partition(".")
    if setup.ontology.has_type(type_name):
        return {f"{type_name}.{name}"}
    return {f"{found}.{name}" for found in setup.ontology.abstract.get(type_name, ())}


def _feature_properties(model: Mapping[str, Any] | None) -> list[str]:
    """The properties a model record reads as features: each a bare `Type.property` or
    the JSON of a feature naming one."""
    found = []
    for text in (model or {}).get("features") or []:
        text = str(text)
        if text.lstrip().startswith("{"):
            text = (records.loads(text, {}) or {}).get("property") or ""
        if text:
            found.append(text)
    return found


def _input_properties(setup: Setup, question: QuestionSetup) -> set[str]:
    """What a predictive question computes from: its target's cells and its active
    model's features, as concrete `Type.property`."""
    texts = {f"{type_name}.{name}" for type_name, name in question.cells}
    for text in _feature_properties(question.model):
        texts |= _concrete(setup, text)
    return texts


def _feature_inputs(setup: Setup, question: QuestionSetup) -> set[str]:
    """The properties a question's active model reads as features, as concrete
    `Type.property`. An unknown value of one, of whichever object, keeps every series of
    an origin from being forecast; an unknown value of the target only its own series,
    which is suspect and left out as every suspect series is."""
    texts: set[str] = set()
    for text in _feature_properties(question.model):
        texts |= _concrete(setup, text)
    return texts


def _unanswered(setup: Setup, property_text: str) -> list[str]:
    """The questions of the contract that compute from a property: its targets, or its
    active model's target and features."""
    contract = setup.manifest.contract
    found = set()
    for qid, question in sorted(((contract.questions if contract else None) or {}).items()):
        texts: set[str] = set()
        for text in question.targets or []:
            texts |= _concrete(setup, text)
        if qid in setup.questions:
            texts |= _input_properties(setup, setup.questions[qid])
        if property_text in texts:
            found.add(qid)
    return sorted(found)


@dataclass(frozen=True)
class _Context:
    twin: Twin
    setup: Setup
    now: datetime
    predictor: Predictor
    started: float
    deadline_s: float
    spans: dict[tuple[str, str], list[tuple]]


def _measure(context: _Context, question: QuestionSetup, earlier: list[Scored]) -> Measured:
    setup, now = context.setup, context.now
    model = question.model
    entry: dict[str, Any] = {
        "model": model["uid"] if model else None,
        "fitted_to": records.iso(question.fitted_to),
        "metric": question.metric,
        "tolerance": question.tolerance,
        "horizon_steps": question.steps,
        "origin": None,
        "scored": False,
        "reason": None,
    }
    measured = Measured(question, entry, origins=list(earlier))
    if question.reason is not None:
        entry["reason"] = _reason(*question.reason)
        return measured
    assert model is not None and question.steps is not None and question.tolerance is not None
    origin = reading.floor_to(now - question.horizon, setup.interval)
    entry["origin"] = iso_utc(origin)
    if any(found.origin == origin for found in earlier):
        entry["reason"] = _reason("already_scored", "this origin was scored at an earlier check")
        return measured
    fitted_to = question.fitted_to
    if fitted_to is None:
        entry["reason"] = _reason("not_fitted", f"{model['uid']} records no end of its data")
        return measured
    if origin < fitted_to:
        entry["reason"] = _reason(
            "before_fit", f"the origin is before the end of the data {model['uid']} was fitted on"
        )
        return measured
    version = reading.version_at(context.twin, origin)
    if version is None:
        entry["reason"] = _reason("no_version", "the live line has no version at the origin")
        return measured
    inputs = _feature_inputs(setup, question)
    withheld = sorted(
        records.signal_uid(property_text, uid)
        for (property_text, uid), spans in context.spans.items()
        if property_text in inputs and _unknown_at(spans, origin)
    )
    if withheld:
        log.info(
            "an input of the question was unknown at the origin; not scored",
            question=question.id,
            origin=iso_utc(origin),
            signals=withheld,
        )
        entry["reason"] = _reason(
            "input_unknown",
            f"the value of {', '.join(withheld)} was unknown at the origin; nothing is "
            "computed from it",
        )
        entry["withheld"] = withheld
        return measured
    grid = [origin + j * setup.step for j in range(1, question.steps + 1)]
    if grid[-1] > setup.head.t:
        entry["reason"] = _reason(
            "ahead_of_data", "the origin's horizon reaches past the newest version"
        )
        return measured
    left = context.deadline_s - (time.monotonic() - context.started)
    if left <= 0:
        entry["reason"] = _reason("deadline", "the check's budget ran out before it was asked")
        return measured
    interventions = (
        reading.interventions(context.twin, setup.head, origin, grid[-1])
        if question.interventional
        else []
    )
    asked = time.perf_counter()
    try:
        table = context.predictor(
            model,
            origin=version,
            horizon=question.steps,
            step=setup.step,
            interventions=interventions,
            deadline_s=left,
        )
    except DtaError as exc:
        log.warn(
            "the predictor refused an origin; the question is not scored at this check",
            question=question.id,
            model=model["uid"],
            origin=iso_utc(origin),
            code=exc.code,
        )
        entry["reason"] = _reason("predictor_refused", f"{exc.code}: {exc.message}")
        return measured
    except Exception as exc:
        log.error(
            "the predictor failed; the question is not scored at this check",
            exc=exc,
            question=question.id,
            model=model["uid"],
            origin=iso_utc(origin),
        )
        entry["reason"] = _reason(
            "predictor_failed", f"{type(exc).__name__} was raised; the cause is in the log"
        )
        return measured
    try:
        predicted = _prediction(table, grid)
    except _Bad as exc:
        log.warn(
            "a prediction cannot be scored",
            question=question.id,
            model=model["uid"],
            origin=iso_utc(origin),
            reason=str(exc),
        )
        entry["reason"] = _reason("bad_prediction", str(exc))
        return measured
    log.debug(
        "origin predicted",
        question=question.id,
        model=model["uid"],
        origin=iso_utc(origin),
        series=len(predicted),
        interventions=len(interventions),
        elapsed_ms=_ms(asked),
    )
    last = reading.micros(grid[-1])
    measured.estimates = {
        uid: values[last][0] for uid, values in sorted(predicted.items()) if last in values
    }
    measured.estimates_at = grid[-1]
    entry["estimates"] = measured.estimates
    entry["estimates_at"] = iso_utc(grid[-1])
    widths = [
        (hi - lo) / question.tolerance
        for values in predicted.values()
        for _, lo, hi in values.values()
        if lo is not None and hi is not None
    ]
    entry["interval_ratio"] = sum(widths) / len(widths) if widths else None

    observed = reading.observed(
        context.twin,
        setup.head,
        question.cells,
        version,
        sorted(predicted),
        [*grid, now] if now not in grid else grid,
    )
    measured.types = dict(observed.types)
    moment_now = reading.micros(now)
    measured.current = {uid: observed.values.get((uid, moment_now)) for uid in observed.types}
    left_out = sorted(
        uid
        for uid, type_name in observed.types.items()
        if _suspect_in(
            context.spans.get((f"{type_name}.{question.cells[0][1]}", uid), ()), origin, grid[-1]
        )
    )
    entry["left_out"] = left_out
    excluded = set(left_out)
    names: list[str] = []
    ys: list[float] = []
    yhats: list[float] = []
    for uid in sorted(predicted):
        if uid in excluded:
            continue
        for moment, (mean, _, _) in sorted(predicted[uid].items()):
            value = observed.values.get((uid, moment))
            if value is None or not math.isfinite(value):
                continue
            names.append(uid)
            ys.append(value)
            yhats.append(mean)
    if not names:
        entry["reason"] = _reason(
            "no_observations", "no predicted point has an observed value in force"
        )
        return measured
    try:
        score = measures.score_origin(names, ys, yhats, question.metric or "")
    except measures.Undefined as exc:
        entry["reason"] = _reason("metric_undefined", str(exc))
        return measured
    entry.update(scored=True, error=score.error, n=score.n, series=score.residuals)
    measured.scored = Scored(
        origin=origin,
        at=now,
        error=score.error,
        n=score.n,
        model=model["uid"],
        fitted_to=entry["fitted_to"],
        residuals=score.residuals,
    )
    measured.origins = [*earlier, measured.scored]
    return measured


@dataclass(frozen=True)
class _Stuck:
    test: quality.StuckTest
    value: Any
    # The intervals of the lookback with a change, and where the lookback begins.
    changed: tuple[bool, ...] = ()
    start: datetime | None = None
    step: timedelta | None = None

    def changes_since(self, moment: datetime) -> int:
        """Intervals that began at or after `moment` and had a change."""
        if self.start is None or self.step is None:
            return 0
        first = max(0, -((self.start - moment) // self.step))
        return sum(1 for flag in self.changed[first:] if flag)


def _raw_readings(
    twin: Twin,
    setup: Setup,
    first: VersionRef,
    spans: Mapping[tuple[str, str], list[tuple]],
    start: datetime,
) -> dict[tuple[str, str], tuple[list[tuple], list[tuple[datetime, Any]]]]:
    """Per (Type.property, uid) suspect at some time after `start`: its spans and the raw
    readings its signal record kept, as (time, value), from the record's history."""
    wanted = {
        records.signal_uid(*key): key
        for key, found in spans.items()
        if any(until is None or until > start for _, until, *_ in found)
    }
    if not wanted or not setup.ontology.has_type(SIGNAL_TYPE):
        return {}
    events = inforce.events(
        twin.store,
        setup.ontology,
        SIGNAL_TYPE,
        first,
        setup.head,
        properties=["reading"],
        uids=sorted(wanted),
    )
    raw: dict[tuple[str, str], list[tuple[datetime, Any]]] = {}
    for uid, moment, text in zip(
        events.column("uid").to_pylist(),
        events.column(inforce.T).to_pylist(),
        events.column("reading").to_pylist(),
        strict=True,
    ):
        if text is not None:
            raw.setdefault(wanted[uid], []).append((moment, records.loads(text)))
    return {
        key: ([(found[0], found[1]) for found in spans[key]], sorted(raw.get(key, [])))
        for key in wanted.values()
    }


def _lookbacks(
    twin: Twin, setup: Setup, now: datetime, spans: Mapping[tuple[str, str], list[tuple]]
) -> dict[tuple[str, str], _Stuck]:
    """The stuck test of every observed signal over the lookback: the fidelity window,
    never fewer than `quality.STUCK_LOOKBACK` intervals. Inside the times a signal was
    suspect it reads the raw readings, never what the check wrote in their place."""
    if not setup.observations:
        return {}
    started = time.perf_counter()
    count = setup.window // setup.step
    if count <= 0:
        return {}
    count = max(count, quality.STUCK_LOOKBACK)
    start = now - count * setup.step
    first = reading.version_in_force(twin, start) or reading.first_version(twin)
    ends = [start + (index + 1) * setup.step for index in range(count)]
    source_ids = sorted({source for found in setup.sources_of.values() for source in found})
    delivering = reading.delivery(twin, setup.head, first, source_ids, ends, setup.step)
    raw = _raw_readings(twin, setup, first, spans, start)
    by_type: dict[str, list[str]] = {}
    for type_name, name in setup.observations:
        by_type.setdefault(type_name, []).append(name)
    results: dict[tuple[str, str], _Stuck] = {}
    for type_name, names in by_type.items():
        withheld = {
            (key[0].partition(".")[2], key[1]): found
            for key, found in raw.items()
            if key[0].partition(".")[0] == type_name
        }
        found = reading.lookback(
            twin, setup.head, first, type_name, names, start, setup.step, count, withheld
        )
        for (name, uid), seen in found.items():
            sources = setup.sources_of.get((type_name, name), ())
            delivered = [
                any(delivering[source][index] for source in sources) for index in range(count)
            ]
            results[(f"{type_name}.{name}", uid)] = _Stuck(
                quality.stuck_test(seen.changed, delivered, seen.steps),
                seen.value,
                tuple(seen.changed),
                start,
                setup.step,
            )
    log.debug(
        "observed signals tested for being stuck",
        signals=len(results),
        intervals=count,
        elapsed_ms=_ms(started),
    )
    return results


# -- deciding ------------------------------------------------------------------------------


@dataclass
class _Measurement:
    setup: Setup
    now: datetime
    uid: str
    questions: dict[str, Measured]
    earlier: list[dict[str, Any]]
    stuck: dict[tuple[str, str], _Stuck]
    freshness: Mapping[str, Freshness]
    sources: dict[str, dict[str, Any]]
    coverage: dict[str, Any]
    trace_id: str
    clock: float
    # Per signal flagged as the measurement found it: the forecast that stands in for its
    # readings, made before the lock (`_infer`).
    inference: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class _Current:
    head: VersionRef
    incidents: list[dict[str, Any]]
    signals: dict[str, dict[str, Any]]
    state: dict[str, Any] | None
    snapshot: dict[str, dict[str, str]]


@dataclass
class _Decision:
    upserts: dict[str, list[dict[str, Any]]]
    inferred: dict[str, dict[str, Provenance]]
    inferred_rows: dict[str, list[dict[str, Any]]]
    check_row: dict[str, Any]
    signals_document: dict[str, Any]


def _active(measured: Measured) -> list[Scored]:
    """The origins the active model, as fitted now, predicted after the end of its data."""
    question = measured.setup
    if question.model is None:
        return []
    uid, fitted_to = question.model["uid"], question.fitted_to
    text = records.iso(fitted_to)
    return [
        found
        for found in measured.origins
        if found.by(uid, text) and (fitted_to is None or found.origin >= fitted_to)
    ]


def _step_entry(entry: Mapping[str, Any], now: datetime, check: str) -> dict[str, Any]:
    return {"at": iso_utc(now), "check": check, **entry}


def _apply_step(
    document: dict[str, Any],
    step: ladder.Step,
    question: QuestionSetup,
    *,
    now: datetime,
    check: str,
    snapshot: Mapping[str, str],
    settings: Any,
) -> dict[str, Any]:
    position = step.position
    if step.request is not None:
        rung, attempt, cycle = step.request
        name = ladder.RUNG_NAMES[rung]
        key = correction_key(document["uid"], cycle, name, attempt)
        position = replace(position, pending=ladder.Pending(key, rung, attempt, cycle, now))
        logged = [
            *step.logged,
            {"action": "request", "key": key, "rung": name, "attempt": attempt, "cycle": cycle},
        ]
    else:
        logged = list(step.logged)
    updated = records.with_position(document, position)
    detail = dict(updated["detail"])
    if step.remodel_requested:
        detail["remodel"] = {
            "requested_at": iso_utc(now),
            "timeout_s": settings.remodel_timeout.total_seconds(),
            "models": dict(snapshot),
        }
    if position.status == RESOLVED:
        updated["resolved_at"] = now
    updated["detail"] = detail
    # Each step says the cycle it belongs to and when the next cycle is due, if it is.
    ladder_fields = {"cycle": position.cycle, "next_cycle_at": iso(position.due)}
    updated["steps"] = [
        *(document.get("steps") or []),
        *(_step_entry({**ladder_fields, **item}, now, check) for item in logged),
    ]
    return updated


def _new_incident(
    kind: str,
    subject: str,
    *,
    now: datetime,
    version: VersionRef,
    detail: dict[str, Any],
    taken: set[str],
    rung: int = 0,
    cycle: int = 0,
) -> dict[str, Any]:
    number = 1
    uid = records.incident_uid(kind, subject, now)
    while uid in taken:
        number += 1
        uid = records.incident_uid(kind, subject, now, number)
    taken.add(uid)
    return {
        "uid": uid,
        "kind": kind,
        "subject": subject,
        "status": OPEN,
        "opened_at": now,
        "opened_version": version.id,
        "resolved_at": None,
        "rung": rung,
        "attempt": 0,
        "cycle": cycle,
        "awaiting": None,
        "requested_at": None,
        "detail": detail,
        "steps": [],
    }


def _on_property(measurement: _Measurement, property_text: str) -> list[Measured]:
    """The predictive questions that target a property, shortest horizon first."""
    type_name, _, name = property_text.partition(".")
    return sorted(
        (
            found
            for found in measurement.questions.values()
            if (type_name, name) in found.setup.cells
        ),
        key=lambda found: (found.setup.horizon, found.setup.id),
    )


def _inferring(measurement: _Measurement, property_text: str) -> Measured | None:
    """The question that would infer a signal of a property: of those with an active
    model, the one with the shortest horizon."""
    return next(
        (
            found
            for found in _on_property(measurement, property_text)
            if found.setup.model is not None
        ),
        None,
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _observed_range(
    context: _Context, property_text: str, uid: str, tolerance: float | None
) -> dict[str, Any]:
    """The range an inferred value of a signal may lie in: per side, the ontology's
    declared bound, else the bound observed over the window (outside the times the
    signal was suspect) widened by the margin."""
    setup, now = context.setup, context.now
    type_name, _, name = property_text.partition(".")
    declared = (
        setup.ontology.type(type_name).field(name)
        if setup.ontology.has_type(type_name) and setup.ontology.type(type_name).has_field(name)
        else None
    )
    first = reading.version_in_force(context.twin, now - setup.window) or reading.first_version(
        context.twin
    )
    events = inforce.events(
        context.twin.store,
        setup.ontology,
        type_name,
        first,
        setup.head,
        properties=[name],
        uids=[uid],
    )
    spans = context.spans.get((property_text, uid), ())
    seen = [
        number
        for moment, value in zip(
            events.column(inforce.T).to_pylist(), events.column(name).to_pylist(), strict=True
        )
        if not any(
            since <= moment and (until is None or moment < until) for since, until, *_ in spans
        )
        and (number := _number(value)) is not None
        and math.isfinite(number)
    ]
    lowest, highest = (min(seen), max(seen)) if seen else (None, None)
    margin = None
    if lowest is not None and highest is not None:
        margin = max(RANGE_MARGIN * (highest - lowest), float(tolerance or 0.0))
    low = declared.minimum if declared is not None and declared.minimum is not None else None
    high = declared.maximum if declared is not None and declared.maximum is not None else None
    return {
        "low": float(low) if low is not None else (None if margin is None else lowest - margin),
        "high": float(high) if high is not None else (None if margin is None else highest + margin),
        "declared": {"low": low is not None, "high": high is not None},
        "observed": [lowest, highest],
        "margin": margin,
    }


def _infer(context: _Context, measurement: _Measurement, property_text: str, uid: str) -> dict:
    """The forecast that stands in for a signal's readings while it is suspect, made once
    when it is flagged: by the inferring question's active model, from the version in
    force one step before the check (the readings up to the one before the flagged one;
    every other input as it was in force then), for the question's horizon. Never from a
    version that holds an inferred value of this signal. `refused` says why there is
    none."""
    setup, now = context.setup, context.now
    chosen = _inferring(measurement, property_text)
    if chosen is None or chosen.setup.model is None or chosen.setup.steps is None:
        return {
            "refused": _reason(
                "no_question", "no predictive question with an active model targets the property"
            )
        }
    question, model = chosen.setup, chosen.setup.model
    origin = reading.floor_to(now - setup.step, setup.interval)
    version = reading.version_at(context.twin, origin)
    if version is None:
        return {"refused": _reason("no_version", "the live line has no version at the origin")}
    inputs = _feature_inputs(setup, question)
    withheld = sorted(
        records.signal_uid(text, found)
        for (text, found), spans in context.spans.items()
        if text in inputs and _unknown_at(spans, origin)
    )
    if withheld:
        log.warn(
            "an input of the inferring question is unknown at the origin; the flagged "
            "signal's value is unknown",
            signal=records.signal_uid(property_text, uid),
            question=question.id,
            signals=withheld,
        )
        return {
            "refused": _reason(
                "input_unknown",
                f"the value of {', '.join(withheld)} was unknown at the origin; nothing is "
                "computed from it",
            )
        }
    grid = [origin + j * setup.step for j in range(1, question.steps + 1)]
    left = context.deadline_s - (time.monotonic() - context.started)
    if left <= 0:
        return {"refused": _reason("deadline", "the check's budget ran out before it was asked")}
    interventions = (
        reading.interventions(context.twin, setup.head, origin, grid[-1])
        if question.interventional
        else []
    )
    try:
        table = context.predictor(
            model,
            origin=version,
            horizon=question.steps,
            step=setup.step,
            interventions=interventions,
            deadline_s=left,
        )
        path = _prediction(table, grid).get(uid) or {}
    except DtaError as exc:
        log.warn(
            "the predictor refused the forecast of a flagged signal; its value is unknown",
            property=property_text,
            object=uid,
            model=model["uid"],
            code=exc.code,
        )
        return {"refused": _reason("predictor_refused", f"{exc.code}: {exc.message}")}
    except _Bad as exc:
        log.warn(
            "the forecast of a flagged signal cannot be read; its value is unknown",
            property=property_text,
            object=uid,
            model=model["uid"],
            reason=str(exc),
        )
        return {"refused": _reason("bad_prediction", str(exc))}
    except Exception as exc:
        log.error(
            "the predictor failed on the forecast of a flagged signal; its value is unknown",
            exc=exc,
            property=property_text,
            object=uid,
            model=model["uid"],
        )
        return {
            "refused": _reason(
                "predictor_failed", f"{type(exc).__name__} was raised; the cause is in the log"
            )
        }
    values = {
        iso_utc(moment): path[reading.micros(moment)][0]
        for moment in grid
        if reading.micros(moment) in path
    }
    log.info(
        "flagged signal forecast from what was observed",
        property=property_text,
        object=uid,
        question=question.id,
        model=model["uid"],
        origin=iso_utc(origin),
        steps=question.steps,
        values=len(values),
    )
    return {
        "question": question.id,
        "model": model["uid"],
        "tolerance": question.tolerance,
        "origin": iso_utc(origin),
        "origin_version": version.id,
        "horizon_steps": question.steps,
        "values": values,
        "range": _observed_range(context, property_text, uid, question.tolerance),
    }


@dataclass(frozen=True)
class _Value:
    """What a suspect signal's value is at a check."""

    kind: str
    value: float | None = None
    t: datetime | None = None
    lead: int | None = None
    why: str | None = None
    message: str | None = None


def _value_now(document: Mapping[str, Any], setup: Setup, now: datetime) -> _Value:
    """A suspect signal's value at a check: the forecast made when it was flagged, at
    the lead time it has reached, while that lead is within the horizon the inferring
    question was validated for and the value is finite and in range; else unknown, and
    unknown it stays until the signal clears."""
    detail = document.get("detail") or {}
    if isinstance(detail.get("unknown"), Mapping):
        return _Value(UNKNOWN)
    inference = detail.get("inference") or {}
    refused_by = inference.get("refused")
    origin = records.parse_time(inference.get("origin"))
    steps = inference.get("horizon_steps")
    if refused_by or origin is None or not isinstance(steps, int):
        why = (refused_by or {}).get("message") or "no forecast was made when it was flagged"
        return _Value(TURN_UNKNOWN, why=records.NO_INFERENCE, message=why)
    lead = (reading.floor_to(now, setup.step) - origin) // setup.step
    if lead > steps:
        return _Value(
            TURN_UNKNOWN,
            lead=lead,
            why=records.HORIZON_PASSED,
            message=f"the lead time of {lead} steps is past the {steps} steps the inferring "
            "question was validated for",
        )
    lead = max(lead, 1)
    moment = origin + lead * setup.step
    value = _number((inference.get("values") or {}).get(iso_utc(moment)))
    if value is None or not math.isfinite(value):
        return _Value(
            TURN_UNKNOWN,
            lead=lead,
            why=records.NOT_FINITE,
            message=f"the forecast has no finite value at {iso_utc(moment)}",
        )
    bounds = inference.get("range") or {}
    low, high = _number(bounds.get("low")), _number(bounds.get("high"))
    if (low is not None and value < low) or (high is not None and value > high):
        return _Value(
            TURN_UNKNOWN,
            value=value,
            lead=lead,
            why=records.OUT_OF_RANGE,
            message=f"the forecast {value:g} lies outside the range [{low}, {high}] the "
            "property can have",
        )
    return _Value(INFERRED, value=value, t=moment, lead=lead)


@dataclass
class _Flags:
    flags: dict[str, dict[str, Any]]
    tested_stuck: int
    tested_biased: int
    alarming: dict[str, dict[str, Any]]
    uncorroborated: list[dict[str, Any]]


def _flags(measurement: _Measurement, suspect_before: set[str]) -> _Flags:
    """The signals the measurement calls stuck or biased, of those not suspect already: a
    plain function of the measurement, asked before the lock (to forecast what is
    flagged) and under it (to decide)."""
    setup, now = measurement.setup, measurement.now
    flags: dict[str, dict[str, Any]] = {}
    tested_stuck = 0
    for (property_text, uid), result in sorted(measurement.stuck.items()):
        signal = records.signal_uid(property_text, uid)
        if signal in suspect_before:
            continue
        tested_stuck += int(result.test.tested)
        if result.test.stuck:
            flags[signal] = {
                "property": property_text,
                "object": uid,
                "reason": STUCK,
                "value": result.value,
                "test": result.test.document(),
                "question": None,
            }
    observed_cells = set(setup.observations)
    alarming: dict[str, dict[str, Any]] = {}
    bias_tests: dict[str, dict[str, quality.BiasTest]] = {}
    called_by: dict[str, set[str]] = {}
    tested_biased = 0
    for qid, measured in sorted(measurement.questions.items()):
        question = measured.setup
        if measured.scored is None or question.tolerance is None:
            continue
        window_origins = [
            found
            for found in _active(measured)
            if measures.in_window(
                found.origin, now=now, horizon=question.horizon, window=setup.window
            )
        ]
        tests: dict[str, quality.BiasTest] = {}
        for uid in sorted(measured.scored.residuals):
            type_name = measured.types.get(uid)
            if type_name is None or (type_name, question.cells[0][1]) not in observed_cells:
                continue
            property_text = f"{type_name}.{question.cells[0][1]}"
            if records.signal_uid(property_text, uid) in suspect_before:
                continue
            history = [found.residuals[uid] for found in window_origins if uid in found.residuals]
            tests[uid] = quality.bias_test(history, question.tolerance)
        called = quality.biased(tests)
        bias_tests[qid], called_by[qid] = tests, called
        tested = sum(1 for found in tests.values() if found.tested)
        tested_biased += tested
        alarming[qid] = {
            "tested": tested,
            "alarming": sum(1 for found in tests.values() if found.alarm),
            "biased": sorted(called),
        }
    # A series is called by the question that would infer it, and is biased only when
    # every other question on its property bears its alarm out.
    uncorroborated: list[dict[str, Any]] = []
    for qid, called in sorted(called_by.items()):
        measured = measurement.questions[qid]
        for uid in sorted(called):
            property_text = f"{measured.types[uid]}.{measured.setup.cells[0][1]}"
            signal = records.signal_uid(property_text, uid)
            if signal in flags:
                continue
            inferring = _inferring(measurement, property_text)
            if inferring is None or inferring.setup.id != qid:
                log.debug(
                    "series alarms in a question that would not infer it; not called",
                    signal=signal,
                    question=qid,
                    inferring=None if inferring is None else inferring.setup.id,
                )
                continue
            test = bias_tests[qid][uid]
            others = {
                found.setup.id: bias_tests.get(found.setup.id, {}).get(uid)
                for found in _on_property(measurement, property_text)
                if found.setup.id != qid
            }
            unsupported = quality.uncorroborated(test, others)
            if unsupported:
                uncorroborated.extend(
                    {
                        "signal": signal,
                        "property": property_text,
                        "object": uid,
                        "question": qid,
                        "by": other,
                        "why": why,
                    }
                    for other, why in unsupported.items()
                )
                log.info(
                    "series alarms but another question on its property does not bear it "
                    "out; not flagged",
                    signal=signal,
                    question=qid,
                    side=test.side,
                    uncorroborated=unsupported,
                )
                continue
            flags[signal] = {
                "property": property_text,
                "object": uid,
                "reason": BIASED,
                "value": measured.current.get(uid),
                "test": test.document(),
                "question": qid,
            }
    return _Flags(flags, tested_stuck, tested_biased, alarming, uncorroborated)


def _decide(measurement: _Measurement, current: _Current) -> _Decision:
    setup, now, check = measurement.setup, measurement.now, measurement.uid
    settings = setup.correction
    taken = {document["uid"] for document in current.incidents}
    by_key = {(document["kind"], document["subject"]): document for document in current.incidents}
    changed: dict[str, dict[str, Any]] = {}
    entries: dict[str, dict[str, Any]] = {}

    # -- questions: window, breach incidents, warnings -------------------------------------
    for qid, measured in measurement.questions.items():
        question = measured.setup
        entry = dict(measured.entry)
        fitted_to = question.fitted_to
        tolerance = question.tolerance
        # Only what the active model, as fitted now, predicted after the end of its data:
        # a recalibration keeps the model's uid and moves the end of its data.
        usable = [(found.origin, found.error) for found in _active(measured)]
        is_breach = False
        if question.model is not None and tolerance is not None:
            error, count = measures.window_error(
                usable, now=now, horizon=question.horizon, window=setup.window, fitted_to=fitted_to
            )
            is_breach = measures.breach(error, count, tolerance)
            entry["window"] = {"error": error, "n": count, "breach": is_breach}
        else:
            entry["window"] = None
        incident = by_key.get((BREACH, qid))
        had_incident = incident is not None
        if incident is None and is_breach:
            window = entry["window"]
            incident = _new_incident(
                BREACH,
                qid,
                now=now,
                version=measurement.setup.head,
                detail={
                    "question": qid,
                    "spec": question.spec,
                    "tolerance": tolerance,
                    "opened": {"error": window["error"], "n": window["n"], "check": check},
                },
                taken=taken,
                rung=1,
                cycle=1,
            )
            incident["steps"] = [
                _step_entry(
                    {
                        "action": "opened",
                        "error": window["error"],
                        "n": window["n"],
                        "cycle": 1,
                        "next_cycle_at": None,
                    },
                    now,
                    check,
                )
            ]
            log.info(
                "breach incident opened",
                incident=incident["uid"],
                question=qid,
                window_error=window["error"],
                origins=window["n"],
                tolerance=tolerance,
            )
        if incident is not None and tolerance is not None:
            position = records.position_of(incident)
            cut = ladder.cut_of(position, incident["opened_at"], fitted_to)
            found = measures.evidence(usable, cut)
            remodel = (incident.get("detail") or {}).get("remodel") or {}
            remodelled = position.status == REMODEL_REQUESTED and ladder.remodelled(
                remodel.get("models") or {}, current.snapshot.get(question.spec, {})
            )
            step = ladder.next_step(
                position,
                settings,
                found,
                now=now,
                tolerance=tolerance,
                remodelled=remodelled,
                interval=setup.interval,
            )
            updated = _apply_step(
                incident,
                step,
                question,
                now=now,
                check=check,
                snapshot=current.snapshot.get(question.spec, {}),
                settings=settings,
            )
            detail = dict(updated["detail"])
            if entry.get("window"):
                detail["now"] = {
                    "error": entry["window"]["error"],
                    "n": entry["window"]["n"],
                    "evidence": {"n": found.n, "error": found.error, "cut": iso(found.cut)},
                    "check": check,
                }
            updated["detail"] = detail
            changed[updated["uid"]] = updated
            if step.logged or step.request:
                log.info(
                    "breach incident stepped",
                    incident=updated["uid"],
                    question=qid,
                    status=updated["status"],
                    rung=updated["rung"],
                    attempt=updated["attempt"],
                    cycle=updated["cycle"],
                    steps=[item["action"] for item in step.logged],
                    request=step.request,
                )
            incident = updated
        # The warning: a drift caught before it breaches, while no breach incident is open.
        previous = reading.warning_state(measurement.earlier, qid)
        same_model = previous.model == entry["model"] and previous.fitted_to == iso(fitted_to)
        total = previous.total if same_model else 0.0
        raised = False
        request: CorrectionRequest | None = None
        last = previous.last
        open_now = incident is not None and incident["status"] in UNRESOLVED
        if had_incident or open_now or tolerance is None:
            total = 0.0
        elif measured.scored is not None:
            total = measures.warning_sum(total, measured.scored.error, tolerance)
            if not is_breach and measures.warning_due(total, last, now=now, window=setup.window):
                raised = True
                pseudo = records.warning_id(qid, now)
                request = CorrectionRequest(
                    incident=pseudo,
                    question=qid,
                    spec=question.spec,
                    rung="recalibrate",
                    cycle=0,
                    attempt=0,
                    key=correction_key(pseudo, 0, "recalibrate", 0),
                    warning=True,
                )
                log.info(
                    "warning raised: a pre-emptive recalibration is asked for",
                    question=qid,
                    warning_sum=total,
                    key=request.key,
                )
                total, last = 0.0, now
        entry["warning"] = {
            "sum": total,
            "raised": raised,
            "last": iso(last),
            "request": request.document() if request else None,
            "fitted_to": iso(fitted_to),
        }
        entries[qid] = entry

    # -- stale incidents -----------------------------------------------------------------
    for qid in sorted(measurement.freshness):
        freshness = measurement.freshness[qid]
        incident = by_key.get((STALE, qid))
        status = incident["status"] if incident else None
        count = int(incident.get("attempt") or 0) if incident else 0
        step = ladder.stale_step(status, count, freshness.fresh, budget=settings.retry_budget)
        if not step.logged:
            continue
        now_detail = {
            "age_s": freshness.age_s,
            "staleness_s": freshness.staleness_s,
            "source": freshness.source,
            "check": check,
        }
        if incident is None:
            incident = _new_incident(
                STALE,
                qid,
                now=now,
                version=measurement.setup.head,
                detail={"question": qid, "opened": now_detail},
                taken=taken,
            )
        detail = {**(incident.get("detail") or {}), "now": now_detail}
        if step.status == FAILED and incident["status"] != FAILED:
            detail["failed_at"] = iso_utc(now)
        updated = {
            **incident,
            "status": step.status,
            "attempt": step.count,
            "detail": detail,
            "steps": [
                *(incident.get("steps") or []),
                *(_step_entry(item, now, check) for item in step.logged),
            ],
        }
        if step.status == RESOLVED:
            updated["resolved_at"] = now
        changed[updated["uid"]] = updated
        log.info(
            "stale incident stepped",
            incident=updated["uid"],
            question=qid,
            status=updated["status"],
            stale_checks=step.count,
        )

    # -- signals -------------------------------------------------------------------------
    signals = {uid: dict(document) for uid, document in current.signals.items()}
    suspect_before = {uid for uid, document in signals.items() if document["status"] == SUSPECT}
    found_flags = _flags(measurement, suspect_before)
    flags = found_flags.flags
    tested_stuck, tested_biased = found_flags.tested_stuck, found_flags.tested_biased
    alarming, uncorroborated = found_flags.alarming, found_flags.uncorroborated

    def incident_of(signal: str) -> dict[str, Any] | None:
        found = by_key.get((SIGNAL, signal))
        if found is None:
            found = next(
                (
                    document
                    for document in changed.values()
                    if document["kind"] == SIGNAL and document["subject"] == signal
                ),
                None,
            )
        return None if found is None else changed.get(found["uid"], found)

    changed_signals: dict[str, dict[str, Any]] = {}
    flagged_entries = []
    for signal, flag in sorted(flags.items()):
        document = signals.get(signal) or {
            "uid": signal,
            "property": flag["property"],
            "object": flag["object"],
            "detail": {},
        }
        detail = dict(document.get("detail") or {})
        episodes = list(detail.get("episodes") or [])
        episodes.append(
            {"since": iso_utc(now), "until": None, "reason": flag["reason"], "cleared_by": None}
        )
        inference = measurement.inference.get(signal) or {
            "refused": _reason("not_forecast", "the signal was not flagged when it was measured")
        }
        detail.update(
            test=flag["test"],
            question=flag["question"],
            episodes=episodes,
            inference=inference,
            unknown=None,
            estimate=None,
        )
        updated = {
            **document,
            "status": SUSPECT,
            "reason": flag["reason"],
            "since": now,
            "value_at_flag": flag["value"],
            "clear_checks": 0,
            "detail": detail,
        }
        signals[signal] = updated
        changed_signals[signal] = updated
        flagged_entries.append(
            {
                "signal": signal,
                "property": flag["property"],
                "object": flag["object"],
                "reason": flag["reason"],
                "detail": flag["test"],
            }
        )
        if (SIGNAL, signal) not in by_key:
            incident = _new_incident(
                SIGNAL,
                signal,
                now=now,
                version=measurement.setup.head,
                detail={
                    "signal": signal,
                    "property": flag["property"],
                    "object": flag["object"],
                    "reason": flag["reason"],
                    "opened": flag["test"],
                },
                taken=taken,
            )
            incident["steps"] = [
                _step_entry({"action": "flagged", "reason": flag["reason"]}, now, check)
            ]
            changed[incident["uid"]] = incident
        log.info(
            "signal flagged as suspect",
            signal=signal,
            property=flag["property"],
            object=flag["object"],
            reason=flag["reason"],
            question=flag["question"],
            inferred_from=inference.get("origin"),
            not_inferred=(inference.get("refused") or {}).get("code"),
        )

    cleared: list[str] = []
    for signal in sorted(suspect_before):
        document = signals[signal]
        since = document.get("since")
        previous = int(document.get("clear_checks") or 0)
        if document.get("reason") == STUCK:
            # Cleared when the raw readings change again the way the test calls normal:
            # the same readings it was flagged on, so a stop has to come again, a whole
            # run of it, before it is flagged again.
            result = measurement.stuck.get((document["property"], document["object"]))
            if result is None or since is None:
                log.debug("no raw readings of a stuck signal at this check", signal=signal)
                continue
            changes = result.changes_since(since)
            sound = (
                changes >= STUCK_CLEAR_CHANGES
                and result.test.n < quality.STUCK_MIN_INTERVALS
                and not result.test.stuck
            )
            count, needed = (1 if sound else 0), 1
            evidence = {"changes": changes, "run": result.test.n}
        else:
            # A biased signal: its reading against its inferred value, while there is one.
            value = _value_now(document, setup, now)
            raw = _number(document.get("reading"))
            reading_at = document.get("reading_at")
            tolerance = _number(
                ((document.get("detail") or {}).get("inference") or {}).get("tolerance")
            )
            if (
                value.kind != INFERRED
                or value.value is None
                or raw is None
                or tolerance is None
                or reading_at is None
                or since is None
                or reading_at <= since
            ):
                log.debug(
                    "no inferred value or fresh reading to clear a biased signal against; "
                    "neither counted nor reset",
                    signal=signal,
                    value=value.kind,
                )
                continue
            sound = abs(raw - value.value) <= CLEAR_TOLERANCES * tolerance
            count, needed = (previous + 1 if sound else 0), CLEAR_CHECKS
            evidence = {"reading": raw, "inferred": value.value, "tolerance": tolerance}
        if count >= needed:
            detail = dict(document.get("detail") or {})
            episodes = [dict(item) for item in detail.get("episodes") or []]
            if episodes and episodes[-1].get("until") is None:
                episodes[-1].update(until=iso_utc(now), cleared_by="check")
            detail.update(episodes=episodes, unknown=None, estimate=None)
            updated = {**document, "status": records.OK, "clear_checks": 0, "detail": detail}
            cleared.append(signal)
            incident = incident_of(signal)
            if incident is not None:
                closed = {**incident, "status": RESOLVED, "resolved_at": now}
                closed["steps"] = [
                    *(closed.get("steps") or []),
                    _step_entry({"action": "cleared", "by": "check", **evidence}, now, check),
                ]
                changed[closed["uid"]] = closed
            log.info("suspect signal cleared", signal=signal, checks=count, **evidence)
        elif count != previous:
            updated = {**document, "clear_checks": count}
        else:
            continue
        signals[signal] = updated
        changed_signals[signal] = updated

    # The value of every signal suspect after the check: the forecast made when it was
    # flagged while within its horizon and range, else unknown (written once, as no value).
    inferred: dict[str, dict[str, Provenance]] = {}
    # One row per object: two suspect properties of one object are written together.
    rows_by_object: dict[tuple[str, str], dict[str, Any]] = {}
    inferred_entries = []
    unknown_entries = []
    for signal in sorted(uid for uid, document in signals.items() if document["status"] == SUSPECT):
        document = signals[signal]
        type_name, _, name = document["property"].partition(".")
        if not setup.ontology.has_type(type_name):
            continue
        value = _value_now(document, setup, now)
        if value.kind == UNKNOWN:
            continue
        detail = dict(document.get("detail") or {})
        inference = detail.get("inference") or {}
        row = rows_by_object.setdefault(
            (type_name, document["object"]), {"uid": document["object"]}
        )
        if value.kind == INFERRED:
            assert value.value is not None and value.t is not None
            field_kind = setup.ontology.type(type_name).field(name).kind
            number = round(value.value) if field_kind == Kind.integer else float(value.value)
            row[name] = number
            inferred.setdefault(document["object"], {})[name] = Provenance(
                source=inference.get("model") or records.ACTOR,
                kind=FactKind.inferred,
                t_valid=value.t,
            )
            estimate = {
                "value": number,
                "t": iso_utc(value.t),
                "question": inference.get("question"),
                "model": inference.get("model"),
                "tolerance": inference.get("tolerance"),
                "origin": inference.get("origin"),
                "lead_steps": value.lead,
            }
            detail["estimate"] = estimate
            inferred_entries.append(
                {
                    "signal": signal,
                    "property": document["property"],
                    "object": document["object"],
                    **{key: estimate[key] for key in ("value", "t", "question", "model")},
                    "origin": estimate["origin"],
                    "lead_steps": value.lead,
                }
            )
        else:
            # Nothing is inferred any more: the property holds no value, said by the
            # signal as its source, until the signal clears.
            row[name] = None
            inferred.setdefault(document["object"], {})[name] = Provenance(
                source=signal, kind=FactKind.inferred, t_valid=now, confidence=0.0
            )
            questions = _unanswered(setup, document["property"])
            state = {
                "since": iso_utc(now),
                "why": value.why,
                "message": value.message,
                "questions": questions,
            }
            episodes = [dict(item) for item in detail.get("episodes") or []]
            if episodes and episodes[-1].get("until") is None:
                episodes[-1]["unknown_since"] = iso_utc(now)
            detail.update(unknown=state, estimate=None, episodes=episodes)
            entry = {
                "signal": signal,
                "property": document["property"],
                "object": document["object"],
                **state,
            }
            unknown_entries.append(entry)
            incident = incident_of(signal)
            if incident is not None:
                stepped = {
                    **incident,
                    "detail": {
                        **(incident.get("detail") or {}),
                        "unknown": state,
                        "unanswered": questions,
                    },
                }
                stepped["steps"] = [
                    *(stepped.get("steps") or []),
                    _step_entry(
                        {
                            "action": "unknown",
                            "why": value.why,
                            "message": value.message,
                            "questions": questions,
                        },
                        now,
                        check,
                    ),
                ]
                changed[stepped["uid"]] = stepped
            log.warn(
                "a suspect signal's value is unknown from now on; nothing computes from it",
                signal=signal,
                property=document["property"],
                object=document["object"],
                why=value.why,
                questions=questions,
            )
        updated = {**document, "detail": detail}
        signals[signal] = updated
        changed_signals[signal] = updated

    inferred_rows: dict[str, list[dict[str, Any]]] = {}
    for (type_name, _), row in sorted(rows_by_object.items()):
        inferred_rows.setdefault(type_name, []).append(row)

    # -- state -----------------------------------------------------------------------------
    statuses = {document["uid"]: document["status"] for document in current.incidents}
    statuses.update({uid: document["status"] for uid, document in changed.items()})
    before = records.state_document(current.state)["state"]
    after = ladder.state_of(statuses.values(), checked=True)
    state_row: dict[str, Any] = {"uid": STATE_UID, "check": check}
    if after != before or current.state is None:
        state_row.update(state=after, since=now)
        log.info("the twin's fidelity state moved", previous=before, state=after, check=check)

    signals_document = {
        "tested": {"stuck": tested_stuck, "biased": tested_biased},
        "flagged": flagged_entries,
        "cleared": cleared,
        "suspect": sorted(
            uid for uid, document in signals.items() if document["status"] == SUSPECT
        ),
        "inferred": inferred_entries,
        "unknown": unknown_entries,
        "alarming": alarming,
        "uncorroborated": uncorroborated,
        "skipped": [],
    }
    freshness_document = {
        "questions": {
            qid: {
                "fresh": found.fresh,
                "age_s": found.age_s,
                "staleness_s": found.staleness_s,
                "source": found.source,
            }
            for qid, found in sorted(measurement.freshness.items())
        },
        "sources": measurement.sources,
    }
    check_row = {
        "uid": check,
        "at": now,
        "at_version": setup.head.id,
        "state": after,
        "questions": dumps(entries),
        "freshness": dumps(freshness_document),
        "signals": dumps(signals_document),
        "coverage": dumps(measurement.coverage),
        "duration_ms": max(0, round((time.perf_counter() - measurement.clock) * 1000.0)),
        "trace_id": measurement.trace_id,
    }
    upserts = {
        CHECK_TYPE: [check_row],
        FIDELITY_TYPE: [state_row],
        INCIDENT_TYPE: [records.incident_row(document) for _, document in sorted(changed.items())],
        SIGNAL_TYPE: [
            records.signal_row(document) for _, document in sorted(changed_signals.items())
        ],
    }
    return _Decision(upserts, inferred, inferred_rows, check_row, signals_document)


# -- reading an outcome back -------------------------------------------------------------


def _pairs(
    twin: Twin,
    type_name: str,
    columns: list[str],
    make: Any,
    version: VersionRef,
    parent: VersionRef,
) -> list[tuple[dict[str, Any] | None, dict[str, Any]]]:
    store = twin.store
    after_rows = store.changes(type_name, range=version.id, columns=columns).to_pylist()
    after = {row["uid"]: make(row) for row in after_rows if row.get("_change") != "removed"}
    if not after:
        return []
    before_rows = store.table(
        type_name, columns=columns, filter=[("uid", "in", sorted(after))], at=parent
    ).to_pylist()
    before = {row["uid"]: make(row) for row in before_rows}
    return [(before.get(uid), document) for uid, document in sorted(after.items())]


def outcome_at(twin: Twin, version: VersionRef, uid: str, *, replayed: bool) -> CheckOutcome:
    """The outcome of the check `uid`, written by `version`, read from the records before
    and after it."""
    store = twin.store
    found = store.get(uid, at=version)
    assert found is not None
    check_document = records.check_document({"uid": uid, **found.values})
    parent = store.resolve(f"#{version.seq - 1}")[0]
    before = records.state_document(reading.state_row(twin, parent))
    after = records.state_document(reading.state_row(twin, version))
    pairs = _pairs(
        twin, INCIDENT_TYPE, records.INCIDENT_COLUMNS, records.incident_document, version, parent
    )
    signal_pairs = _pairs(
        twin, SIGNAL_TYPE, records.SIGNAL_COLUMNS, records.signal_document, version, parent
    )
    events, grouped = incident_events(pairs, check=uid, at=check_document["at"])
    events.extend(unknown_events(signal_pairs, check=uid))
    moved = state_event(before["state"], after["state"], since=after["since"], check=uid)
    if moved is not None:
        events.append(moved)
    corrections = corrections_of(pairs)
    for qid in sorted(check_document["questions"]):
        warning = (check_document["questions"][qid] or {}).get("warning") or {}
        if isinstance(warning.get("request"), Mapping):
            corrections.append(CorrectionRequest(**warning["request"]))
    flagged = [
        dict(document)
        for previous, document in signal_pairs
        if document["status"] == SUSPECT and (previous is None or previous["status"] != SUSPECT)
    ]
    cleared = [
        dict(document)
        for previous, document in signal_pairs
        if document["status"] == records.OK
        and previous is not None
        and previous["status"] == SUSPECT
    ]
    return CheckOutcome(
        check=check_document,
        state_before=before["state"],
        state_after=after["state"],
        opened=grouped["opened"],
        changed=grouped["changed"],
        resolved=grouped["resolved"],
        flagged=flagged,
        cleared=cleared,
        corrections=corrections,
        events=events,
        version=version,
        replayed=replayed,
    )


def _replay(twin: Twin, uid: str) -> CheckOutcome:
    history = twin.store.history(uid, properties=[]).to_pylist()
    added = next((row for row in history if row["_change"] == "added"), None)
    if added is None:
        log.error("a check record has no version that added it", check=uid)
        raise refused(BAD_REQUEST, f"the version that wrote {uid} cannot be found", check=uid)
    version = twin.store.resolve(added["_version"])[0]
    log.info(
        "check made before at this time; its outcome is given again", check=uid, seq=version.seq
    )
    return outcome_at(twin, version, uid, replayed=True)


# -- the check -----------------------------------------------------------------------------


def _commit(
    twin: Twin, decision: _Decision, now: datetime, check: str, trace_id: str
) -> VersionRef:
    upserts = {name: rows for name, rows in decision.upserts.items() if rows}
    if decision.inferred_rows:
        try:
            return records.write(
                twin,
                {**upserts, **decision.inferred_rows},
                now=now,
                cause="derived",
                note=f"fidelity check {check}",
                extra={"fidelity": "check", "check": check},
                inferred=decision.inferred,
                trace=trace_id,
            )
        except ViolationError as exc:
            log.warn(
                "the store refused the inferred values of suspect signals; the check is "
                "written without them",
                check=check,
                reason=exc.message,
                violations=len(exc.detail.get("violations") or []),
                rows=sum(len(rows) for rows in decision.inferred_rows.values()),
            )
            # The signals keep what was decided (an unknown value stays unknown to every
            # reader of the signal); only the property values are not written.
            document = dict(decision.signals_document)
            document["skipped"] = [
                {"signal": entry["signal"], "reason": "refused by the store"}
                for entry in [*document["inferred"], *document.get("unknown", [])]
            ]
            document["inferred"] = []
            upserts[CHECK_TYPE] = [{**decision.check_row, "signals": dumps(document)}]
    return records.write(
        twin,
        upserts,
        now=now,
        cause="derived",
        note=f"fidelity check {check}",
        extra={"fidelity": "check", "check": check},
        trace=trace_id,
    )


def check(
    twin: Twin,
    now: datetime,
    *,
    predictor: Predictor,
    freshness: Mapping[str, Freshness],
    trace_id: str,
    deadline_s: float,
) -> CheckOutcome:
    """Measure the twin at `now`, decide, and write it all as one version at `now`.

    A check at a time that already has a record gives that record's outcome back and
    writes nothing. Refuses `fidelity.not_live` for a static twin, `fidelity.out_of_order`
    for a time before the latest check's."""
    with trace(trace_id):
        return _check(twin, records.aware(now), predictor, freshness, trace_id, deadline_s)


def _check(
    twin: Twin,
    now: datetime,
    predictor: Predictor,
    freshness: Mapping[str, Freshness],
    trace_id: str,
    deadline_s: float,
) -> CheckOutcome:
    started, clock = time.monotonic(), time.perf_counter()
    store = twin.store
    head = store.head()
    setup = reading.setup(twin, head)
    uid = records.check_uid(now)
    if store.get(uid, at=head) is not None:
        return _replay(twin, uid)
    latest = reading.latest_check_time(twin, head)
    if latest is not None and latest > now:
        log.warn("check refused: a later check exists", now=iso_utc(now), latest=iso_utc(latest))
        raise refused(
            OUT_OF_ORDER,
            f"a check was made at {iso_utc(latest)}, after {iso_utc(now)}",
            now=iso_utc(now),
            latest=iso_utc(latest),
        )
    log.info(
        "fidelity check started",
        check=uid,
        head_seq=head.seq,
        questions=sorted(setup.questions),
        observations=len(setup.observations),
        deadline_s=deadline_s,
    )
    unresolved = reading.unresolved_incidents(twin, head)
    lower = now - setup.window
    for incident in unresolved:
        if incident["kind"] == BREACH:
            lower = min(lower, incident["opened_at"], incident.get("awaiting") or now)
            failed_at = records.parse_time((incident.get("detail") or {}).get("failed_at"))
            if failed_at is not None:
                lower = min(lower, failed_at)
    earlier = reading.prior_checks(twin, head, lower, now)
    signals = reading.signal_documents(twin, head)
    context = _Context(twin, setup, now, predictor, started, deadline_s, _spans(signals))
    questions = {
        qid: _measure(context, question, reading.scored_origins(earlier, qid))
        for qid, question in setup.questions.items()
    }
    stuck = _lookbacks(twin, setup, now, context.spans)
    unknown = sorted(set(freshness) - set(setup.questions))
    if unknown:
        log.warn("freshness given for questions the contract does not ask; ignored", ids=unknown)
    measurement = _Measurement(
        setup=setup,
        now=now,
        uid=uid,
        questions=questions,
        earlier=earlier,
        stuck=stuck,
        freshness={qid: found for qid, found in freshness.items() if qid in setup.questions},
        sources={
            source: {
                name: iso(value) if isinstance(value, datetime) else value
                for name, value in found.items()
            }
            for source, found in reading.sources(twin, head).items()
        },
        coverage=reading.coverage(twin, head, setup.question_status, now, setup.window),
        trace_id=trace_id,
        clock=clock,
    )
    # What this measurement flags is forecast now, outside the lock: the decision under
    # it flags from the same measurement, and a signal it flags that was not forecast here
    # (another writer changed the signals in between) has no value inferred.
    suspect_now = {uid for uid, document in signals.items() if document["status"] == SUSPECT}
    for signal, flag in sorted(_flags(measurement, suspect_now).flags.items()):
        measurement.inference[signal] = _infer(
            context, measurement, flag["property"], flag["object"]
        )
    measured_ms = _ms(clock)
    with WRITES:
        head = store.head()
        if store.get(uid, at=head) is not None:
            return _replay(twin, uid)
        current = _Current(
            head=head,
            incidents=reading.unresolved_incidents(twin, head),
            signals=reading.signal_documents(twin, head),
            state=reading.state_row(twin, head),
            snapshot=reading.model_snapshot(
                twin, head, [question.spec for question in setup.questions.values()]
            ),
        )
        decision = _decide(measurement, current)
        version = _commit(twin, decision, now, uid, trace_id)
    outcome = outcome_at(twin, version, uid, replayed=False)
    scored = sorted(qid for qid, found in questions.items() if found.scored is not None)
    log.info(
        "fidelity check done",
        check=uid,
        seq=version.seq,
        state=outcome.state_after,
        previous=outcome.state_before,
        scored=scored,
        not_scored={
            qid: (found.entry.get("reason") or {}).get("code")
            for qid, found in questions.items()
            if found.scored is None
        },
        opened=[found["uid"] for found in outcome.opened],
        resolved=[found["uid"] for found in outcome.resolved],
        corrections=[found.key for found in outcome.corrections],
        flagged=[found["uid"] for found in outcome.flagged],
        events=[found["type"] for found in outcome.events],
        measured_ms=measured_ms,
        elapsed_ms=_ms(clock),
    )
    return outcome
