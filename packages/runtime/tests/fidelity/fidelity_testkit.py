"""A small live plant for the fidelity tests: its ontology and manifest, a sync done by
hand one step at a time, the twin's records of its questions and models, and a
predictor whose errors the test chooses.

The plant: gauges that read a level, a flow and further readings, pumps that are
running or not. What the source reads at step i (time T0 + i * STEP) is, unless a test
says otherwise:

    level of gauge Gk     10 + k + 0.5 * (i % 4)      changes at every step
    flow of gauge Gk      2k + (i % 3)                changes at every step
    rN of gauge Gk        k + N + 0.25 * ((i + N) % 5)
    running of pump Pk    1 while (i // 6) is even, else 0: it seldom changes

STEP is both the sync interval and the fidelity interval. The predictor knows these
rules: with no offset it predicts them exactly.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow as pa
import yaml
from dta_core.errors import DtaError
from dta_core.manifest import Manifest, parse_manifest
from dta_core.ontology import parse_ontology, type_signature
from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance, StoreSettings, VersionRef
from dta_runtime import fidelity
from dta_runtime.fidelity import CheckOutcome, Freshness
from dta_runtime.protocol.worker import InterventionRef
from dta_runtime.twin import Bundle, Twin, install_twin

T0 = datetime(2026, 1, 5, tzinfo=UTC)
STEP = timedelta(minutes=15)
WINDOW_STEPS = 24
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
TIME = pa.timestamp("us", tz="UTC")
SOURCE = "scada"
EXTRA = tuple(f"r{number}" for number in range(2, 10))

Row = dict[str, Any]


def at(steps: float) -> datetime:
    return T0 + steps * STEP


def index_of(moment: datetime) -> int:
    return round((moment - T0) / STEP)


def gauge(key: str) -> str:
    return f"gauge:{key}"


def pump(key: str) -> str:
    return f"pump:{key}"


def _number(uid: str) -> int:
    return int(uid.split(":", 1)[1][1:])


def level(uid: str, i: int) -> float:
    return 10.0 + _number(uid) + 0.5 * (i % 4)


def flow(uid: str, i: int) -> float:
    return 2.0 * _number(uid) + (i % 3)


def running(uid: str, i: int) -> int:
    return 1 if (i // 6) % 2 == 0 else 0


def _extra(number: int) -> Callable[[str, int], float]:
    def value(uid: str, i: int) -> float:
        return _number(uid) + number + 0.25 * ((i + number) % 5)

    return value


TRUTH: dict[str, Callable[[str, int], Any]] = {
    "level": level,
    "flow": flow,
    "running": running,
    **{name: _extra(int(name[1:])) for name in EXTRA},
}
GAUGE_PROPERTIES = ("level", "flow", *EXTRA)


def ontology_text() -> str:
    readings = "".join(
        f"      {name}:\n        range: float\n        annotations:\n          dta_role: state\n"
        for name in GAUGE_PROPERTIES
    )
    return (
        "id: https://example.org/plant\n"
        "name: plant\n"
        "description: Gauges and pumps of a small plant.\n"
        "prefixes:\n"
        "  linkml: https://w3id.org/linkml/\n"
        "  plant: https://example.org/plant/\n"
        "default_prefix: plant\n"
        "default_range: string\n"
        "imports:\n"
        "  - linkml:types\n"
        "classes:\n"
        "  Gauge:\n"
        "    description: A gauge on the plant.\n"
        "    attributes:\n"
        "      uid:\n"
        "        identifier: true\n"
        "        required: true\n"
        f"{readings}"
        "  Pump:\n"
        "    description: A pump that runs or stands.\n"
        "    attributes:\n"
        "      uid:\n"
        "        identifier: true\n"
        "        required: true\n"
        "      running:\n"
        "        range: integer\n"
        "        annotations:\n"
        "          dta_role: state\n"
    )


def question(
    target: str,
    horizon_steps: int,
    model: str,
    *,
    tolerance: float = 0.5,
    metric: str = "mae",
) -> dict[str, Any]:
    return {
        "text": f"What will {target} be?",
        "kind": "predictive",
        "priority": 1,
        "tolerance": {"metric": metric, "value": tolerance},
        "staleness": 3600,
        "targets": [target],
        "horizon": int((horizon_steps * STEP).total_seconds()),
        "model": model,
    }


def spec(qid: str, target: str, *, interventional: bool = False) -> dict[str, Any]:
    return {
        "question": qid,
        "target": target,
        "candidates": {
            "naive": {"family": "persistence"},
            "seasonal": {"family": "seasonal_naive", "params": {"season_length": 4}},
        },
        "backtest": {"initial_window": 3600, "step": 900},
        "acceptance": {"metric": "mae", "threshold": 0.5},
        "interventional": interventional,
    }


LEVEL_QUESTIONS = {"level": question("Gauge.level", 2, "level")}
LEVEL_MODELS = {"level": spec("level", "Gauge.level")}


def plant_manifest(
    text: str,
    *,
    questions: Mapping[str, Any] = LEVEL_QUESTIONS,
    models: Mapping[str, Any] = LEVEL_MODELS,
    observations: Sequence[str] = ("Gauge.level", "Gauge.flow", "Pump.running"),
    window_steps: int = WINDOW_STEPS,
    correction: Mapping[str, Any] | None = None,
    live: bool = True,
) -> Manifest:
    gauge_columns = {name: {"column": name} for name in GAUGE_PROPERTIES}
    document: dict[str, Any] = {
        "twin": {"id": "plant", "name": "Plant", "revision": 1},
        "contract": {
            "tier": "live" if live else "static",
            "questions": dict(questions),
            "boundary": {
                "state": ["Gauge.level", "Pump.running"],
                "observations": list(observations),
            },
            "notification": {"kind": "log"},
        },
        "ontology": {
            "path": "ontology/plant.yaml",
            "type_signature": type_signature(parse_ontology(text).ontology),
        },
        "sources": {
            SOURCE: {
                "connector": "file",
                "location": "/nowhere",
                "mappings": {
                    "gauges": {
                        "dataset": "gauges",
                        "object_type": "Gauge",
                        "identity": ["id"],
                        "valid_time": "ts",
                        "properties": gauge_columns,
                    },
                    "pumps": {
                        "dataset": "pumps",
                        "object_type": "Pump",
                        "identity": ["id"],
                        "valid_time": "ts",
                        "properties": {"running": {"column": "running"}},
                    },
                },
            }
        },
        "models": dict(models),
    }
    if live:
        document["sync"] = {"interval": int(STEP.total_seconds())}
        document["fidelity"] = {
            "interval": int(STEP.total_seconds()),
            "window": int((window_steps * STEP).total_seconds()),
            "correction": dict(correction or {}),
        }
    return parse_manifest(yaml.safe_load(yaml.safe_dump(document)), origin="test manifest")


@dataclass
class Plant:
    """A live twin of the plant, synced and checked by the test step by step."""

    twin: Twin
    gauges: tuple[str, ...]
    pumps: tuple[str, ...]
    properties: tuple[str, ...] = ("level", "flow")
    # (property, uid) -> what the source reads at step i instead of the truth.
    readings: dict[tuple[str, str], Callable[[int], Any]] = field(default_factory=dict)

    @property
    def store(self):
        return self.twin.store

    def read(self, name: str, uid: str, i: int) -> Any:
        override = self.readings.get((name, uid))
        return override(i) if override is not None else TRUTH[name](uid, i)

    def sync(self, i: int, *, ok: bool = True) -> VersionRef:
        """Step i's readings as a sync writes them: suspect signals are kept on their
        record instead of on the object."""
        t = at(i)
        source: Row = {"uid": f"twin_source:{SOURCE}", "status": "ok" if ok else "failing"}
        upserts: dict[str, list[Row]] = {}
        if ok:
            source.update(watermark=t, last_success_at=t)
            rows = {
                "Gauge": [
                    {"uid": uid, **{name: self.read(name, uid, i) for name in self.properties}}
                    for uid in self.gauges
                ],
                "Pump": [
                    {"uid": uid, "running": self.read("running", uid, i)} for uid in self.pumps
                ],
            }
            kept, readings = fidelity.withhold(rows, fidelity.suspects(self.twin), t=t)
            upserts.update(kept)
            if readings:
                upserts["TwinSignal"] = readings
        upserts["TwinSource"] = [source]
        changes = ChangeSet(
            upserts=upserts,
            provenance=Provenance(source=SOURCE, kind=FactKind.observed, t_valid=t),
        )
        return self.store.commit(changes, CommitMeta(cause="sync", actor="sync", t=t))

    def register(self, rows: Mapping[str, list[Row]]) -> VersionRef:
        """The twin's records about itself, at the time of the head."""
        head = self.store.head()
        changes = ChangeSet(
            upserts=dict(rows),
            provenance=Provenance(source="test", kind=FactKind.inferred, t_valid=head.t),
        )
        return self.store.commit(changes, CommitMeta(cause="registration", actor="test", t=head.t))

    def activate(
        self,
        qid: str,
        target: str,
        *,
        fitted_to: datetime,
        candidate: str = "naive",
        interventional: bool = False,
        spec_id: str | None = None,
    ) -> str:
        """Register a model of the question's specification and make it the active one."""
        uid = model_uid(spec_id or qid, candidate)
        self.register(
            {
                "TwinModel": [
                    model_row(
                        spec_id or qid,
                        candidate,
                        target,
                        fitted_to=fitted_to,
                        interventional=interventional,
                        question=qid,
                    )
                ],
                "TwinQuestion": [
                    {
                        "uid": f"twin_question:{qid}",
                        "question": qid,
                        "status": "validated",
                        "model": uid,
                    }
                ],
            }
        )
        return uid

    def refit(self, uid: str, fitted_to: datetime) -> VersionRef:
        """What a recalibration leaves: the same model, fitted to newer data."""
        return self.register({"TwinModel": [{"uid": uid, "fitted_to": fitted_to}]})

    def check(
        self,
        i: int,
        predictor: Any,
        *,
        freshness: Mapping[str, Freshness] | None = None,
        deadline_s: float = 60.0,
    ) -> CheckOutcome:
        return fidelity.check(
            self.twin,
            at(i),
            predictor=predictor,
            freshness=dict(freshness or {}),
            trace_id=TRACE,
            deadline_s=deadline_s,
        )

    def step(self, i: int, predictor: Any, **kwargs: Any) -> CheckOutcome:
        self.sync(i)
        return self.check(i, predictor, **kwargs)

    def run(self, steps: Iterable[int], predictor: Any, **kwargs: Any) -> dict[int, CheckOutcome]:
        return {i: self.step(i, predictor, **kwargs) for i in steps}


def model_uid(spec_id: str, candidate: str) -> str:
    return f"twin_model:{spec_id}/{candidate}"


def model_row(
    spec_id: str,
    candidate: str,
    target: str,
    *,
    fitted_to: datetime | None,
    status: str = "active",
    interventional: bool = False,
    question: str | None = None,
) -> Row:
    return {
        "uid": model_uid(spec_id, candidate),
        "spec": spec_id,
        "candidate": candidate,
        "family": "persistence" if candidate == "naive" else "seasonal_naive",
        "params": json.dumps({} if candidate == "naive" else {"season_length": 4}),
        "question": question or spec_id,
        "target": target,
        "interventional": interventional,
        "origin": "runtime",
        "status": status,
        "fitted_to": fitted_to,
    }


Offset = Callable[[Mapping[str, Any], str, datetime], float]


def no_offset(model: Mapping[str, Any], uid: str, origin: datetime) -> float:
    return 0.0


class Oracle:
    """Predicts the plant's rules, plus an offset of the test's choosing per model,
    series and origin. Every call is kept."""

    def __init__(
        self,
        plant: Plant,
        offset: Offset = no_offset,
        *,
        series: Sequence[str] | None = None,
        refuse: bool = False,
    ) -> None:
        self.plant = plant
        self.offset = offset
        self.series = series
        self.refuse = refuse
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        model: Mapping[str, Any],
        *,
        origin: VersionRef,
        horizon: int,
        step: timedelta,
        interventions: Sequence[InterventionRef],
        deadline_s: float,
    ) -> pa.Table:
        self.calls.append(
            {
                "model": model["uid"],
                "origin": origin.t,
                "horizon": horizon,
                "step": step,
                "interventions": list(interventions),
                "deadline_s": deadline_s,
            }
        )
        if self.refuse:
            raise DtaError("the worker is not there", code="worker.unavailable")
        name = model["target"].split(".", 1)[1]
        rule = TRUTH[name]
        series = self.series or (self.plant.pumps if name == "running" else self.plant.gauges)
        uids, times, means = [], [], []
        for uid in series:
            for j in range(1, horizon + 1):
                moment = origin.t + j * step
                uids.append(uid)
                times.append(moment)
                means.append(float(rule(uid, index_of(moment))) + self.offset(model, uid, origin.t))
        mean = pa.array(means, pa.float64())
        return pa.table(
            {
                "unique_id": pa.array(uids, pa.string()),
                "ds": pa.array(times, TIME),
                "mean": mean,
                "lo_80": pa.array([value - 0.25 for value in means], pa.float64()),
                "hi_80": pa.array([value + 0.25 for value in means], pa.float64()),
            }
        )


def shifted(amount: float, *, since: datetime, uids: Iterable[str] | None = None) -> Offset:
    """Predictions off by `amount` from origin `since` on, for some series or all."""
    chosen = None if uids is None else set(uids)

    def offset(model: Mapping[str, Any], uid: str, origin: datetime) -> float:
        if origin < since or (chosen is not None and uid not in chosen):
            return 0.0
        return amount

    return offset


def install_plant(
    settings: StoreSettings,
    *,
    gauges: Sequence[str] = ("G1", "G2", "G3", "G4"),
    pumps: Sequence[str] = ("P1",),
    properties: Sequence[str] = ("level", "flow"),
    **manifest: Any,
) -> Plant:
    text = ontology_text()
    installed = install_twin(
        settings, Bundle(manifest=plant_manifest(text, **manifest), ontology_text=text), t=T0
    )
    return Plant(
        twin=installed.twin,
        gauges=tuple(gauge(key) for key in gauges),
        pumps=tuple(pump(key) for key in pumps),
        properties=tuple(properties),
    )


def kinds(events: Sequence[Mapping[str, Any]]) -> list[tuple[str, str | None]]:
    """(type, action) of each event, in order; the action is that of an incident event,
    the new state of a state event."""
    found = []
    for event in events:
        data = event["data"]
        found.append((event["type"], data.get("action") or data.get("to")))
    return found
