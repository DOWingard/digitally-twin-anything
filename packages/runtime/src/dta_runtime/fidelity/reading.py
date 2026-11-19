"""What a check reads of the twin: its settings, questions and models, what earlier
checks kept, the observed values in force, the twin's own actions, its sources.

Every read names the version it reads, so what a check decides rests on one head.
The values in force are worked out by `inforce`, the same way the rest of the twin
works them out.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.manifest.checks import ManifestView
from dta_core.manifest.models import Correction, QuestionKind
from dta_core.schema import Kind, Ontology
from dta_core.store import EPOCH, Selector, UnknownVersion, VersionRef

from .. import inforce
from ..protocol.worker import InterventionRef
from ..twin import Twin
from . import records
from .errors import NOT_LIVE, UNSUPPORTED, refused
from .ladder import RESOLVED
from .quality import steps_per_interval
from .records import (
    ACTION_RECORD_TYPE,
    ACTION_TYPE,
    CHECK_TYPE,
    INCIDENT_COLUMNS,
    INCIDENT_TYPE,
    MODEL_TYPE,
    QUESTION_TYPE,
    RUN_TYPE,
    SIGNAL_COLUMNS,
    SIGNAL_TYPE,
    SOURCE_TYPE,
)

log = Log("runtime.fidelity")

ACTIVE = "active"
EXECUTED = "executed"
SOURCE_OK = "ok"
_NUMERIC = (Kind.integer, Kind.float)
_MICROSECOND = timedelta(microseconds=1)
# What makes a candidate the one it is; a re-model changes one of these or adds a candidate.
_IDENTITY = ("family", "code", "params", "target", "features")

Cell = tuple[str, str]


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


def floor_to(moment: datetime, interval: timedelta) -> datetime:
    """The latest multiple of the interval at or before `moment`, counted from the epoch."""
    return EPOCH + ((moment - EPOCH) // interval) * interval


def micros(moment: datetime) -> int:
    return (moment - EPOCH) // _MICROSECOND


# -- the twin's settings -------------------------------------------------------------------


def cells_of(ontology: Ontology, text: str | None) -> tuple[Cell, ...]:
    """(concrete type, property) of every column `Type.property` names; empty when it
    names nothing the ontology holds."""
    if not text or "." not in text:
        return ()
    type_name, _, name = text.partition(".")
    if type_name in ontology.abstract:
        members = tuple(found for found in ontology.abstract[type_name] if ontology.has_type(found))
    elif ontology.has_type(type_name):
        members = (type_name,)
    else:
        return ()
    if not members or not all(ontology.type(found).has_field(name) for found in members):
        return ()
    return tuple((found, name) for found in members)


def numeric(ontology: Ontology, cell: Cell) -> bool:
    found = ontology.type(cell[0]).field(cell[1])
    return found.kind in _NUMERIC and not found.multivalued


@dataclass(frozen=True)
class QuestionSetup:
    """One predictive question with a horizon and a model specification."""

    id: str
    spec: str
    horizon: timedelta
    # The horizon in steps of the grid; None when it is not a whole number of them.
    steps: int | None
    metric: str | None
    tolerance: float | None
    # The active model's record, with its uid.
    model: dict[str, Any] | None
    target: str | None
    cells: tuple[Cell, ...]
    # Why it cannot be scored at all: (code, message).
    reason: tuple[str, str] | None

    @property
    def interventional(self) -> bool:
        return bool(self.model and self.model.get("interventional"))

    @property
    def fitted_to(self) -> datetime | None:
        return records.utc(self.model.get("fitted_to")) if self.model else None


@dataclass(frozen=True)
class Setup:
    head: VersionRef
    manifest: Manifest
    ontology: Ontology
    # The sync interval: the step of every grid.
    step: timedelta
    interval: timedelta
    window: timedelta
    correction: Correction
    questions: dict[str, QuestionSetup]
    # Numeric observed properties, as (concrete type, property).
    observations: tuple[Cell, ...]
    # The sources that map each observed property.
    sources_of: dict[Cell, tuple[str, ...]]
    # Status of every question record: what coverage counts.
    question_status: dict[str, str] = field(default_factory=dict)


def _table(
    twin: Twin, type_name: str, at: VersionRef, ontology: Ontology, **kwargs: Any
) -> list[dict[str, Any]]:
    if not ontology.has_type(type_name):
        return []
    return twin.store.table(type_name, at=at, ontology=ontology, **kwargs).to_pylist()


def require(twin: Twin, head: VersionRef) -> Ontology:
    """The ontology at the head; refused `fidelity.unsupported` when it lacks the
    fidelity records (a twin installed before they existed)."""
    ontology = twin.store.ontology_at(head)
    missing = [name for name in records.TYPES if not ontology.has_type(name)]
    if missing:
        log.warn("fidelity refused: the twin's ontology lacks its records", missing=missing)
        raise refused(
            UNSUPPORTED,
            f"the twin's ontology has no {', '.join(missing)}; install the twin again with "
            "this version to bring its shared types up to date",
            missing=missing,
        )
    return ontology


def setup(twin: Twin, head: VersionRef) -> Setup:
    """The twin's fidelity settings at the head, and every question a check looks at.

    Refuses `fidelity.not_live` for a twin with no sync interval or no fidelity interval
    and window, `fidelity.unsupported` when its ontology lacks the fidelity records."""
    ontology = require(twin, head)
    manifest = twin.manifest(at=head)
    step = manifest.sync.interval if manifest.sync is not None else None
    fidelity = manifest.fidelity
    if step is None or fidelity is None or fidelity.interval is None or fidelity.window is None:
        log.info(
            "fidelity refused: the twin has no recurring sync or no fidelity settings",
            sync_interval=None if step is None else step.total_seconds(),
            fidelity=fidelity is not None,
        )
        raise refused(
            NOT_LIVE,
            "the twin has no sync interval or no fidelity interval and window: a static twin "
            "has no fidelity checks",
        )
    view = ManifestView(manifest)
    stored_questions = {
        row["question"]: row
        for row in _table(
            twin, QUESTION_TYPE, head, ontology, columns=["question", "model", "status"]
        )
    }
    models = {row["uid"]: row for row in _table(twin, MODEL_TYPE, head, ontology)}
    questions: dict[str, QuestionSetup] = {}
    for qid, question in view.questions.items():
        if question.kind != QuestionKind.predictive or question.horizon is None:
            continue
        if not question.model:
            continue
        spec = view.models.get(question.model)
        stored = stored_questions.get(qid)
        model = models.get(stored["model"]) if stored and stored.get("model") else None
        reason: tuple[str, str] | None = None
        if spec is None:
            reason = (
                "no_specification",
                f"the manifest has no model specification {question.model}",
            )
        elif model is None or model.get("status") != ACTIVE:
            reason = ("no_active_model", f"the twin holds no active model for {qid}")
        target = (model or {}).get("target") or (spec.target if spec else None)
        cells = cells_of(ontology, target)
        if reason is None and (not cells or not all(numeric(ontology, cell) for cell in cells)):
            reason = ("bad_target", f"{target} is not a numeric property of the ontology")
        steps = question.horizon // step if question.horizon % step == timedelta(0) else None
        if reason is None and not steps:
            reason = (
                "horizon_not_on_grid",
                "the horizon is not a whole number of sync intervals of "
                f"{step.total_seconds():g} s",
            )
        tolerance = question.tolerance
        metric = tolerance.metric.value if tolerance and tolerance.metric else None
        value = tolerance.value if tolerance else None
        if reason is None and (metric is None or value is None):
            reason = ("no_tolerance", f"{qid} states no tolerance with a metric and a value")
        questions[qid] = QuestionSetup(
            id=qid,
            spec=question.model,
            horizon=question.horizon,
            steps=steps,
            metric=metric,
            tolerance=value,
            model=model,
            target=target,
            cells=cells,
            reason=reason,
        )
    boundary = manifest.contract.boundary if manifest.contract is not None else None
    observations: list[Cell] = []
    for text in (boundary.observations or []) if boundary is not None else []:
        found = cells_of(ontology, text)
        if not found:
            log.warn("an observed property is not in the ontology; it is not tested", property=text)
        for cell in found:
            if numeric(ontology, cell) and cell not in observations:
                observations.append(cell)
    sources_of = {
        cell: tuple(
            sorted({mapped.source_id for mapped in view.mapped_columns(f"{cell[0]}.{cell[1]}")})
        )
        for cell in observations
    }
    return Setup(
        head=head,
        manifest=manifest,
        ontology=ontology,
        step=step,
        interval=fidelity.interval,
        window=fidelity.window,
        correction=fidelity.correction,
        questions=questions,
        observations=tuple(observations),
        sources_of=sources_of,
        question_status={qid: row.get("status") for qid, row in stored_questions.items()},
    )


# -- versions ------------------------------------------------------------------------------


def version_in_force(twin: Twin, moment: datetime) -> VersionRef | None:
    """The last version of the live line whose time is not later; None before the first."""
    try:
        return twin.store.resolve(Selector("time", time=moment))[0]
    except UnknownVersion:
        return None


def version_at(twin: Twin, moment: datetime) -> VersionRef | None:
    """The version in force at a time when its own time is that time; None otherwise."""
    found = version_in_force(twin, moment)
    return found if found is not None and found.t == moment else None


def first_version(twin: Twin) -> VersionRef:
    return twin.store.resolve("#0")[0]


# -- fidelity's own records ----------------------------------------------------------------


def kept_forecasts(
    twin: Twin, at: VersionRef, now: datetime
) -> dict[tuple[str, str], dict[str, Any]]:
    """The forecasts the latest check before `now` kept for the stuck test of smooth
    signals, per (signal uid, origin): a run carries its forecast from check to check."""
    rows = twin.store.table(
        CHECK_TYPE,
        columns=["at", "signals"],
        filter=[("at", "<", now)],
        order_by=[("at", "desc")],
        limit=1,
        at=at,
    ).to_pylist()
    if not rows:
        return {}
    document = records.loads(rows[0]["signals"], {})
    entries = document.get("forecasts") if isinstance(document, dict) else None
    found = {
        (entry["signal"], entry["origin"]): entry
        for entry in entries or []
        if isinstance(entry, dict) and entry.get("signal") and entry.get("origin")
    }
    log.debug("forecasts kept by the latest check read", check=rows[0]["uid"], forecasts=len(found))
    return found


def latest_check_time(twin: Twin, at: VersionRef) -> datetime | None:
    rows = twin.store.table(
        CHECK_TYPE, columns=["at"], order_by=[("at", "desc")], limit=1, at=at
    ).to_pylist()
    return records.utc(rows[0]["at"]) if rows else None


def unresolved_incidents(twin: Twin, at: VersionRef) -> list[dict[str, Any]]:
    rows = twin.store.table(
        INCIDENT_TYPE,
        columns=INCIDENT_COLUMNS,
        filter=[("status", "!=", RESOLVED)],
        at=at,
    ).to_pylist()
    return sorted(
        (records.incident_document(row) for row in rows),
        key=lambda found: (found["opened_at"], found["uid"]),
    )


def signal_documents(twin: Twin, at: VersionRef) -> dict[str, dict[str, Any]]:
    rows = twin.store.table(SIGNAL_TYPE, columns=SIGNAL_COLUMNS, at=at).to_pylist()
    return {row["uid"]: records.signal_document(row) for row in rows}


def prior_checks(
    twin: Twin, at: VersionRef, lower: datetime, now: datetime
) -> list[dict[str, Any]]:
    """The checks made at or after `lower` and before `now`, oldest first: their time and
    what they kept per question."""
    started = time.perf_counter()
    rows = twin.store.table(
        CHECK_TYPE,
        columns=["at", "questions"],
        filter=[("at", ">=", lower), ("at", "<", now)],
        at=at,
    ).to_pylist()
    found = sorted(
        (
            {
                "uid": row["uid"],
                "at": records.utc(row["at"]),
                "questions": records.loads(row["questions"], {}),
            }
            for row in rows
        ),
        key=lambda document: document["at"],
    )
    log.debug("earlier checks read", checks=len(found), elapsed_ms=_ms(started))
    return found


@dataclass(frozen=True)
class Scored:
    """One origin of one question scored at an earlier check, or at this one."""

    origin: datetime
    at: datetime
    error: float
    n: int
    model: str | None
    # End of the data of the model that predicted it, as ISO text.
    fitted_to: str | None
    residuals: Mapping[str, float]

    def by(self, model: str | None, fitted_to: str | None) -> bool:
        """Whether this model, fitted to this time, predicted it."""
        return self.model == model and self.fitted_to == fitted_to


def scored_origins(checks: Iterable[Mapping[str, Any]], question: str) -> list[Scored]:
    found: dict[datetime, Scored] = {}
    for document in checks:
        entry = (document.get("questions") or {}).get(question)
        if not entry or not entry.get("scored") or entry.get("error") is None:
            continue
        origin = records.parse_time(entry.get("origin"))
        if origin is None or origin in found:
            continue
        found[origin] = Scored(
            origin=origin,
            at=document["at"],
            error=float(entry["error"]),
            n=int(entry.get("n") or 0),
            model=entry.get("model"),
            fitted_to=entry.get("fitted_to"),
            residuals={
                str(uid): float(value) for uid, value in (entry.get("series") or {}).items()
            },
        )
    return sorted(found.values(), key=lambda scored: scored.origin)


@dataclass(frozen=True)
class WarningState:
    total: float
    last: datetime | None
    model: str | None
    fitted_to: str | None


def warning_state(checks: Sequence[Mapping[str, Any]], question: str) -> WarningState:
    """The warning sum and the latest warning as the newest earlier check left them."""
    for document in reversed(checks):
        entry = (document.get("questions") or {}).get(question)
        if not entry or not isinstance(entry.get("warning"), Mapping):
            continue
        warning = entry["warning"]
        return WarningState(
            total=float(warning.get("sum") or 0.0),
            last=records.parse_time(warning.get("last")),
            model=entry.get("model"),
            fitted_to=warning.get("fitted_to"),
        )
    return WarningState(0.0, None, None, None)


def identity(row: Mapping[str, Any]) -> str:
    """A digest of what makes a candidate the one it is."""
    document = {name: row.get(name) for name in _IDENTITY}
    if isinstance(document.get("features"), list | tuple):
        document["features"] = list(document["features"])
    text = json.dumps(document, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def model_snapshot(twin: Twin, at: VersionRef, specs: Iterable[str]) -> dict[str, dict[str, str]]:
    """Per specification, the identity of each of its candidates."""
    wanted = sorted(set(specs))
    if not wanted:
        return {}
    rows = twin.store.table(
        MODEL_TYPE, columns=["spec", *_IDENTITY], filter=[("spec", "in", wanted)], at=at
    ).to_pylist()
    snapshot: dict[str, dict[str, str]] = {spec: {} for spec in wanted}
    for row in rows:
        snapshot.setdefault(row["spec"], {})[row["uid"]] = identity(row)
    return snapshot


def state_row(twin: Twin, at: VersionRef) -> dict[str, Any] | None:
    found = twin.store.get(records.STATE_UID, at=at)
    if found is None or found.type != records.FIDELITY_TYPE:
        return None
    return {"uid": found.uid, **found.values}


# -- observed values -----------------------------------------------------------------------


@dataclass
class Observed:
    """The values in force of a target at times, per series: (uid, time in microseconds)
    to value, and the concrete type of each series."""

    values: dict[tuple[str, int], float | None] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)


def observed(
    twin: Twin,
    head: VersionRef,
    cells: Sequence[Cell],
    first: VersionRef,
    uids: Sequence[str],
    times: Sequence[datetime],
) -> Observed:
    """The values in force of the target's cells for these series at these times."""
    found = Observed()
    if not uids or not times:
        return found
    asked = pa.array(list(times), inforce.TIME)
    for type_name, name in cells:
        events = inforce.events(
            twin.store, twin.ontology, type_name, first, head, properties=[name], uids=list(uids)
        )
        if events.num_rows == 0:
            continue
        table = inforce.at_times(events, asked)
        moments = table.column("_t").cast(pa.int64()).to_pylist()
        for uid, moment, value in zip(
            table.column("uid").to_pylist(), moments, table.column(name).to_pylist(), strict=True
        ):
            found.values[(uid, moment)] = None if value is None else float(value)
            found.types[uid] = type_name
    return found


def interventions(
    twin: Twin, head: VersionRef, start: datetime, end: datetime
) -> list[InterventionRef]:
    """The twin's own executed actions whose effect falls inside (start, end], as the
    interventions a model is asked to hold under. An action that carries out no control
    (no target property) or sets no `value` is not one."""
    ontology = twin.store.ontology_at(head)
    if not ontology.has_type(ACTION_RECORD_TYPE) or not ontology.has_type(ACTION_TYPE):
        return []
    executed = _table(
        twin,
        ACTION_RECORD_TYPE,
        head,
        ontology,
        columns=["action", "object", "params", "effective_at", "status"],
        filter=[
            ("status", "=", EXECUTED),
            ("effective_at", ">", start),
            ("effective_at", "<=", end),
        ],
    )
    if not executed:
        return []
    actions = {
        row["uid"]: row
        for row in _table(
            twin,
            ACTION_TYPE,
            head,
            ontology,
            columns=["target"],
            filter=[("uid", "in", sorted({row["action"] for row in executed}))],
        )
    }
    placed: list[tuple[datetime, str, InterventionRef]] = []
    for row in executed:
        target = (actions.get(row["action"]) or {}).get("target")
        params = records.loads(row.get("params"), {})
        if not target or not isinstance(params, Mapping) or "value" not in params:
            log.debug(
                "an executed action is not an intervention: it sets no control value",
                record=row["uid"],
                action=row["action"],
            )
            continue
        moment = records.utc(row["effective_at"])
        assert moment is not None
        placed.append(
            (
                moment,
                row["uid"],
                InterventionRef(
                    target=target, uid=row["object"], value=params["value"], start=moment
                ),
            )
        )
    placed.sort(key=lambda found: (found[0], found[1]))
    # Each holds until the next one on the same property of the same object.
    result: list[InterventionRef] = []
    for index, (_, _, found) in enumerate(placed):
        later = next(
            (
                other.start
                for _, _, other in placed[index + 1 :]
                if (other.target, other.uid) == (found.target, found.uid)
            ),
            None,
        )
        result.append(found.model_copy(update={"end": later}) if later is not None else found)
    return result


# -- sources and observed signals ----------------------------------------------------------


def sources(twin: Twin, head: VersionRef) -> dict[str, dict[str, Any]]:
    """Each source's status and how far it has been read, by source id."""
    rows = twin.store.table(
        SOURCE_TYPE, columns=["status", "watermark", "last_success_at"], at=head
    ).to_pylist()
    prefix = "twin_source:"
    return {
        row["uid"].removeprefix(prefix): {
            "status": row.get("status"),
            "watermark": records.utc(row.get("watermark")),
            "last_success_at": records.utc(row.get("last_success_at")),
        }
        for row in rows
    }


def delivery(
    twin: Twin,
    head: VersionRef,
    first: VersionRef,
    source_ids: Sequence[str],
    ends: Sequence[datetime],
    step: timedelta,
) -> dict[str, list[bool]]:
    """For each source and interval end: whether the source was ok and read to within
    one interval of that end, as of that time."""
    result = {source: [False] * len(ends) for source in source_ids}
    if not source_ids or not ends:
        return result
    uids = [f"twin_source:{source}" for source in source_ids]
    events = inforce.events(
        twin.store,
        twin.ontology,
        SOURCE_TYPE,
        first,
        head,
        properties=["status", "watermark"],
        uids=uids,
    )
    if events.num_rows == 0:
        return result
    table = inforce.at_times(events, pa.array(list(ends), inforce.TIME))
    index = {moment: position for position, moment in enumerate(micros(end) for end in ends)}
    moments = table.column("_t").cast(pa.int64()).to_pylist()
    for uid, moment, status, watermark in zip(
        table.column("uid").to_pylist(),
        moments,
        table.column("status").to_pylist(),
        table.column("watermark").to_pylist(),
        strict=True,
    ):
        source = uid.removeprefix("twin_source:")
        position = index.get(moment)
        if position is None or source not in result:
            continue
        end = ends[position]
        result[source][position] = (
            status == SOURCE_OK and watermark is not None and watermark >= end - step
        )
    return result


@dataclass(frozen=True)
class Lookback:
    """What the stuck test reads of one observed property of one object."""

    changed: list[bool]
    # The value in force at the end of the lookback.
    value: Any
    # How far the value moved in each interval; None where it did not change.
    steps: list[float | None] = field(default_factory=list)


# While a signal is suspect the property holds what the check wrote (an inferred value,
# or none) and its readings go to the signal's record: per (property name, uid), the
# times it was suspect, as (since, until), and its raw readings as (time, value).
Withheld = Mapping[tuple[str, str], tuple[Sequence[tuple], Sequence[tuple[datetime, Any]]]]


def _in_spans(moment: datetime, spans: Sequence[tuple]) -> bool:
    return any(span[0] <= moment and (span[1] is None or moment < span[1]) for span in spans)


def _readings_of(
    series: list[tuple[datetime, Any]], withheld: tuple[Sequence[tuple], Sequence[tuple]] | None
) -> list[tuple[datetime, Any]]:
    """The values of a property as its source read them: inside a suspect span, the raw
    readings the signal's record kept in place of what the check wrote."""
    if withheld is None:
        return series
    spans, raw = withheld
    kept = [(moment, value) for moment, value in series if not _in_spans(moment, spans)]
    inside = [
        (moment, value)
        for moment, value in raw
        if _in_spans(moment, spans) and not any(moment == span[0] for span in spans)
    ]
    return sorted([*kept, *inside], key=lambda pair: pair[0])


def lookback(
    twin: Twin,
    head: VersionRef,
    first: VersionRef,
    type_name: str,
    names: Sequence[str],
    start: datetime,
    step: timedelta,
    count: int,
    withheld: Withheld | None = None,
) -> dict[tuple[str, str], Lookback]:
    """Per (property, uid) of a type: in which of `count` intervals after `start` the value
    changed and by how much, and the value at the end. One read of the type
    for all its properties. Inside a signal's suspect spans the raw readings of
    `withheld` stand for what the property held."""
    started = time.perf_counter()
    events = inforce.events(
        twin.store, twin.ontology, type_name, first, head, properties=list(names)
    )
    end = start + count * step
    result: dict[tuple[str, str], Lookback] = {}
    if events.num_rows == 0:
        return result
    order = pc.sort_indices(events, sort_keys=[("uid", "ascending"), (inforce.SEQ, "ascending")])
    events = events.take(order)
    uids = events.column("uid").to_pylist()
    times = events.column(inforce.T).to_pylist()
    removed = events.column(inforce.REMOVED).to_pylist()
    columns = {name: events.column(name).to_pylist() for name in names}
    boundaries = (
        np.flatnonzero(np.asarray(uids[1:], dtype=object) != np.asarray(uids[:-1], dtype=object))
        + 1
    )
    starts = [0, *boundaries.tolist()]
    stops = [*boundaries.tolist(), len(uids)]
    for first_row, stop in zip(starts, stops, strict=True):
        uid = uids[first_row]
        if removed[stop - 1] and times[stop - 1] <= end:
            continue
        for name in names:
            values = columns[name]
            stored: list[tuple[datetime, Any]] = []
            for row in range(first_row, stop):
                moment = times[row]
                if moment > end:
                    break
                stored.append((moment, None if removed[row] else values[row]))
            before = None
            series: list[tuple[datetime, Any]] = []
            for moment, value in _readings_of(stored, (withheld or {}).get((name, uid))):
                if moment <= start:
                    before = value
                else:
                    series.append((moment, value))
            if before is None and not series:
                continue
            steps = steps_per_interval(start, step, count, before, series)
            current = series[-1][1] if series else before
            if current is None:
                continue
            result[(name, uid)] = Lookback([found is not None for found in steps], current, steps)
    log.debug(
        "lookback read",
        type=type_name,
        properties=list(names),
        events=events.num_rows,
        signals=len(result),
        elapsed_ms=_ms(started),
    )
    return result


def coverage(
    twin: Twin, head: VersionRef, statuses: Mapping[str, str], now: datetime, window: timedelta
) -> dict[str, Any]:
    """Questions by status, and the rows the sync runs that ended inside the window set aside."""
    grouped: dict[str, list[str]] = {"validated": [], "provisional": [], "failing": []}
    for qid, status in sorted(statuses.items()):
        if status in grouped:
            grouped[status].append(qid)
    ontology = twin.store.ontology_at(head)
    runs = _table(
        twin,
        RUN_TYPE,
        head,
        ontology,
        columns=["rows_rejected", "finished_at"],
        filter=[("finished_at", ">", now - window), ("finished_at", "<=", now)],
    )
    return {
        **grouped,
        "rows_rejected": int(sum(row.get("rows_rejected") or 0 for row in runs)),
        "runs": len(runs),
    }
