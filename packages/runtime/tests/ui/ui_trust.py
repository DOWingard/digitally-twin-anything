"""Live twins whose fidelity and actions the interface shows, served with their API.

The fidelity world: a plant of four gauges (fidelity_testkit) with two predictive
questions, `level` (two steps ahead, ε 0.5 m) and `flow` (one step ahead, ε 0.5 m3/h).
Synced and checked by the fidelity library from step 1 to 29: the level of G1 reads
stuck from step 22 to 25 (flagged at 24, cleared by the check at 28: a resolved
incident) and the flow of G2 from 22 on (still suspect). From step 30 the checks are
asked for through the API, as the scheduler would ask, so that their notices reach the
API's notifications: the level is predicted 2.0 too high from origin 30, the window
breaches, recalibration and reselection are refused (there is no worker) and the
incident waits for a re-model.

The badge world: the same plant, never checked (unknown), then checked fresh (healthy),
then with a reconcile incident open (correcting), then checked stale with a retry
budget of one (failed).

The crowded world: three questions with at least 300 scored origins each and 50 more
incidents, written in bulk from the records of real checks; most of it in the last 30
days before the head, some of it older.

The acting world: the water twin of the action tests with a fidelity section, its
records in every status written by the action library through a real HTTP executor
against a plant of the test's, a mismatch found by reconciliation, a loop run, and a
thousand more records copied in bulk below them.
"""

from __future__ import annotations

import contextlib
import json
import secrets
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import actions_testkit as acting_kit
import httpx
import pytest
from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance, StoreSettings
from dta_runtime import actions, fidelity
from dta_runtime.api import create_app as create_api
from dta_runtime.clock import FixedClock
from dta_runtime.fidelity import Freshness
from dta_runtime.fidelity.records import check_uid, dumps, incident_uid
from dta_runtime.settings import RuntimeSettings, UiSettings
from dta_runtime.twin import Bundle, Twin, install_twin
from dta_runtime.ui import create_app as create_ui
from fidelity_testkit import (
    Oracle,
    Plant,
    gauge,
    install_plant,
    model_uid,
    question,
    spec,
)
from fidelity_testkit import at as plant_at
from fidelity_testkit import flow as true_flow
from fidelity_testkit import level as true_level
from live_api_testkit import wait_for_job
from ui_testkit import (
    DRIVER_AGENT,
    ApiServer,
    RequestLog,
    ResponseLog,
    UiServer,
    free_port,
    release_port,
)

V1 = "/api/v1"
G1, G2 = gauge("G1"), gauge("G2")
LEVEL_UNIT, FLOW_UNIT = "m", "m3/h"
SHIFT = 2.0
SHIFT_FROM = 30
STUCK_FROM = 22
CLEARED_AT = 28
JOB_S = 120.0
CORRECTION = {"recalibrate_attempts": 1, "retry_budget": 2, "remodel_timeout": 7200}
# The plant and the twin's executors share this secret; the page must never show it.
PLANT_TOKEN = acting_kit.TOKEN


def predictive(
    target: str, horizon_steps: int, model: str, unit: str, priority: int = 1
) -> dict[str, Any]:
    declared = question(target, horizon_steps, model)
    declared["tolerance"] = {**declared["tolerance"], "unit": unit}
    declared["priority"] = priority
    return declared


FIDELITY_QUESTIONS = {
    "level": predictive("Gauge.level", 2, "level", LEVEL_UNIT),
    "flow": predictive("Gauge.flow", 1, "flow", FLOW_UNIT, priority=2),
}
FIDELITY_MODELS = {"level": spec("level", "Gauge.level"), "flow": spec("flow", "Gauge.flow")}


# The one request the page sends that is not a GET: the graph tab asks for a subgraph in
# a body. It reads.
READING_POSTS = {f"{V1}/graph/subgraph"}


def changes_asked(requests: list[Any]) -> list[str]:
    """What the page asked of the API that could change the twin."""
    return [
        f"{request.method} {request.path}"
        for request in requests
        if request.method != "GET" and request.path not in READING_POSTS
    ]


# -- an API and its interface over a twin ------------------------------------------------


@dataclass
class Served:
    """A twin's API and its interface on loopback ports, and the test's own client."""

    twin: Twin
    clock: FixedClock
    token: str
    api: Any
    api_url: str
    ui_url: str
    api_requests: RequestLog
    ui_responses: ResponseLog
    log_file: Path
    driver: httpx.Client
    stops: list[Callable[[], None]] = field(default_factory=list)

    @property
    def services(self) -> Any:
        return self.api.state.services

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        response = self.driver.get(V1 + path, params=params)
        assert response.status_code == 200, response.text
        return response.json()

    def every(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """Every row of a paged listing."""
        rows: list[dict[str, Any]] = []
        cursor = None
        while True:
            page = self.get(path, **params, limit=1000, **({"cursor": cursor} if cursor else {}))
            rows.extend(page["data"])
            cursor = page.get("next_cursor")
            if not cursor:
                return rows

    def job(self, job_id: str) -> dict[str, Any]:
        return wait_for_job(lambda: self.get(f"/lifecycle/jobs/{job_id}")["data"], JOB_S)

    def stop_api(self) -> None:
        """Stop the API only: the interface stays and finds it gone."""
        self.stops.pop(0)()

    def close(self) -> None:
        for stop in reversed(self.stops):
            stop()
        self.stops.clear()


def serve(
    twin: Twin,
    clock: FixedClock,
    root: Path,
    logs: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> Served:
    """Serve a twin's API and interface. `env` is set while the API starts, which is when
    it reads what its executors need."""
    token = secrets.token_urlsafe(32)
    settings = RuntimeSettings(
        store=twin.store.settings,
        api_token=token,
        artifacts_dir=root / "artifacts",
        logs_dir=logs,
    )
    app = create_api(settings, twin, clock=clock, notify_backoff_s=(0.05, 0.1))
    requests = RequestLog(app)
    with pytest.MonkeyPatch.context() as patch:
        for name, value in (env or {}).items():
            patch.setenv(name, value)
        api = ApiServer(requests, free_port()).__enter__()
    api_url = f"http://127.0.0.1:{api.port}"
    ui_app = create_ui(UiSettings(api_url=api_url, api_token=token, logs_dir=logs))
    responses = ResponseLog(ui_app.server)
    ui = UiServer(responses, free_port()).__enter__()
    driver = httpx.Client(
        base_url=api_url,
        headers={"Authorization": f"Bearer {token}", "User-Agent": DRIVER_AGENT},
        timeout=httpx.Timeout(JOB_S),
    )
    served = Served(
        twin=twin,
        clock=clock,
        token=token,
        api=app,
        api_url=api_url,
        ui_url=f"http://127.0.0.1:{ui.port}",
        api_requests=requests,
        ui_responses=responses,
        log_file=logs / "stack.jsonl",
        driver=driver,
    )
    served.stops.append(lambda: api.__exit__(None, None, None))
    served.stops.append(lambda: ui.__exit__(None, None, None))
    served.stops.append(driver.close)
    return served


# -- the fidelity world -------------------------------------------------------------------


def shifted_level(level_model: str) -> Callable[[Mapping[str, Any], str, datetime], float]:
    """The level predicted SHIFT too high from origin SHIFT_FROM on; the flow exactly."""

    def offset(model: Mapping[str, Any], uid: str, origin: datetime) -> float:
        if model["uid"] == level_model and origin >= plant_at(SHIFT_FROM):
            return SHIFT
        return 0.0

    return offset


@dataclass
class FidelityWorld:
    plant: Plant
    served: Served
    oracle: Oracle
    last_step: int

    def check(self, i: int) -> dict[str, Any]:
        """Step i synced, then its check asked for through the API under the scheduler's
        key; once it has ended, the corrections it queued too."""
        self.plant.sync(i)
        self.served.clock.set(plant_at(i))
        response = self.served.driver.post(
            f"{V1}/fidelity/checks",
            headers={"Idempotency-Key": f"fidelity:{plant_at(i).isoformat()}"},
        )
        assert response.status_code in (200, 202), response.text
        checked = self.served.job(response.json()["job"]["id"])
        assert checked["state"] == "succeeded", checked
        for correction in checked["result"]["corrections"]:
            self.served.job(correction["job"])
        return checked


def build_fidelity(settings: StoreSettings, root: Path, logs: Path) -> FidelityWorld:
    plant = install_plant(
        settings, questions=FIDELITY_QUESTIONS, models=FIDELITY_MODELS, correction=CORRECTION
    )
    stuck = range(STUCK_FROM, STUCK_FROM + 4)
    plant.readings[("level", G1)] = lambda i: (
        true_level(G1, STUCK_FROM - 1) if i in stuck else true_level(G1, i)
    )
    plant.readings[("flow", G2)] = lambda i: (
        true_flow(G2, STUCK_FROM - 1) if i >= STUCK_FROM else true_flow(G2, i)
    )
    for i in range(1, 20):
        plant.sync(i)
    level_model = plant.activate("level", "Gauge.level", fitted_to=plant_at(18))
    plant.activate("flow", "Gauge.flow", fitted_to=plant_at(18))
    oracle = Oracle(plant, shifted_level(level_model))
    plant.check(19, oracle)
    plant.run(range(20, SHIFT_FROM), oracle)
    served = serve(plant.twin, FixedClock(plant_at(SHIFT_FROM - 1)), root, logs)
    served.services.check_predictor = lambda services, head: oracle
    world = FidelityWorld(plant=plant, served=served, oracle=oracle, last_step=SHIFT_FROM - 1)
    try:
        for i in range(SHIFT_FROM, SHIFT_FROM + 20):
            world.check(i)
            world.last_step = i
            waiting = served.every("/fidelity/incidents", kind="breach", status="remodel_requested")
            if waiting:
                return world
        raise AssertionError("the breach never came to wait for a re-model")
    except BaseException:
        served.close()
        raise


# -- the badge world ---------------------------------------------------------------------


@dataclass
class BadgeWorld:
    plant: Plant
    served: Served
    step: int = 2

    def _check(self, fresh: bool) -> str:
        self.step += 1
        self.plant.sync(self.step)
        age = 900.0 if fresh else 7200.0
        fidelity.check(
            self.plant.twin,
            plant_at(self.step),
            predictor=Oracle(self.plant, refuse=True),
            freshness={
                "level": Freshness(fresh=fresh, age_s=age, staleness_s=3600.0, source="scada")
            },
            trace_id="ui-badge",
            deadline_s=30.0,
        )
        self.served.clock.set(plant_at(self.step))
        return self.state()

    def healthy(self) -> str:
        return self._check(fresh=True)

    def correcting(self) -> str:
        self.step += 1
        fidelity.open_incident(
            self.plant.twin,
            "reconcile",
            "twin_action_record:elsewhere",
            {"why": "the test opens it"},
            now=plant_at(self.step),
        )
        return self.state()

    def failed(self) -> str:
        return self._check(fresh=False)

    def state(self) -> str:
        return str(fidelity.state(self.plant.twin)["state"])


def build_badge(settings: StoreSettings, root: Path, logs: Path) -> BadgeWorld:
    plant = install_plant(
        settings,
        questions={"level": predictive("Gauge.level", 2, "level", LEVEL_UNIT)},
        models={"level": spec("level", "Gauge.level")},
        correction={"retry_budget": 1},
    )
    for i in range(1, 3):
        plant.sync(i)
    served = serve(plant.twin, FixedClock(plant_at(2)), root, logs)
    return BadgeWorld(plant=plant, served=served)


# -- the crowded world ---------------------------------------------------------------------

CROWDED_QUESTIONS = {
    "level": predictive("Gauge.level", 2, "level", LEVEL_UNIT),
    "flow": predictive("Gauge.flow", 1, "flow", FLOW_UNIT, priority=2),
    "r2": predictive("Gauge.r2", 1, "r2", "1", priority=3),
}
CROWDED_MODELS = {
    name: spec(name, declared["targets"][0]) for name, declared in CROWDED_QUESTIONS.items()
}
SCORED = 300
OLDER = 20
CROWDED_INCIDENTS = 50
INCIDENT_TYPE = "TwinIncident"
CHECK_TYPE = "TwinFidelityCheck"


def _horizon_steps(name: str) -> int:
    return int(CROWDED_QUESTIONS[name]["horizon"]) // 900


def crowded_times(start: datetime) -> list[datetime]:
    """OLDER checks two days apart, then SCORED two hours apart: the last SCORED (and a
    couple of the older) fall in the 30 days before the last."""
    older = [start + timedelta(days=2 * number) for number in range(OLDER)]
    recent_start = older[-1] + timedelta(days=2)
    recent = [recent_start + timedelta(hours=2 * number) for number in range(SCORED)]
    return older + recent


def _bulk(twin: Twin, upserts: Mapping[str, list[dict[str, Any]]], t: datetime) -> None:
    changes = ChangeSet(
        upserts=dict(upserts),
        provenance=Provenance(source="fidelity", kind=FactKind.inferred, t_valid=t),
    )
    twin.store.commit(changes, CommitMeta(cause="derived", actor="fidelity", t=t))


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def build_crowded(settings: StoreSettings, root: Path, logs: Path) -> Served:
    plant = install_plant(
        settings,
        properties=("level", "flow", "r2"),
        questions=CROWDED_QUESTIONS,
        models=CROWDED_MODELS,
        observations=("Gauge.level", "Gauge.flow", "Gauge.r2", "Pump.running"),
    )
    for i in range(1, 6):
        plant.sync(i)
    for name, declared in CROWDED_QUESTIONS.items():
        plant.activate(name, declared["targets"][0], fitted_to=plant_at(4))
    oracle = Oracle(plant)
    plant.run(range(6, 10), oracle)
    template = plant.twin.store.table(CHECK_TYPE, at="head").to_pylist()
    template.sort(key=lambda row: row["at"])
    model_entries = json.loads(template[-1]["questions"])
    checks = []
    times = crowded_times(plant_at(10) + timedelta(days=1))
    for number, moment in enumerate(times):
        questions: dict[str, Any] = {}
        for offset, (name, entry) in enumerate(sorted(model_entries.items())):
            error = round(0.05 + 0.4 * (((number * 7) + offset * 3) % 11) / 10.0, 4)
            window = round(0.1 + 0.2 * ((number + offset) % 5) / 4.0, 4)
            origin = moment - _horizon_steps(name) * timedelta(minutes=15)
            questions[name] = {
                **entry,
                "origin": _iso(origin),
                "scored": True,
                "reason": None,
                "error": error,
                "n": 4,
                "window": {
                    **(entry.get("window") or {}),
                    "error": window,
                    "n": 12,
                    "breach": False,
                },
            }
        checks.append(
            {
                **template[-1],
                "uid": check_uid(moment),
                "at": moment,
                "questions": dumps(questions),
                "duration_ms": 40 + number % 17,
            }
        )
    last = times[-1]
    opened = fidelity.open_incident(
        plant.twin,
        "reconcile",
        "twin_action_record:crowded",
        {"why": "a template"},
        now=plant_at(10),
    )
    stored = plant.twin.store.table(INCIDENT_TYPE, at="head").to_pylist()
    (row,) = [found for found in stored if found["uid"] == opened["uid"]]
    incidents = []
    names = sorted(CROWDED_QUESTIONS)
    for number in range(CROWDED_INCIDENTS):
        moment = times[OLDER + number * 5]
        subject = names[number % len(names)]
        steps = [
            {"at": _iso(moment), "action": "opened", "error": 0.8},
            {
                "at": _iso(moment + timedelta(minutes=15)),
                "action": "request",
                "rung": "recalibrate",
                "attempt": 0,
                "cycle": 1,
            },
            {
                "at": _iso(moment + timedelta(minutes=30)),
                "action": "result",
                "rung": "recalibrate",
                "accepted": True,
                "model": model_uid(subject, "naive"),
                "fitted_to": _iso(moment),
                "error": 0.2,
            },
            {"at": _iso(moment + timedelta(hours=1)), "action": "resolved", "n": 3, "error": 0.2},
        ]
        incidents.append(
            {
                **row,
                "uid": incident_uid("breach", subject, moment, number + 2),
                "kind": "breach",
                "subject": subject,
                "status": "resolved",
                "opened_at": moment,
                "resolved_at": moment + timedelta(hours=1),
                "detail": dumps({"question": subject}),
                "steps": dumps(steps),
            }
        )
    _bulk(plant.twin, {CHECK_TYPE: checks, INCIDENT_TYPE: incidents}, last)
    return serve(plant.twin, FixedClock(last), root, logs)


# -- the acting world ---------------------------------------------------------------------

BULK_RECORDS = 1000
# What each story record was proposed as: (key, action, object, value, state, step).
STORY = (
    ("executed", "set_pump", acting_kit.PUMP, True, "healthy", 2),
    ("proposed", "set_valve", acting_kit.VALVE, 20.0, "healthy", 3),
    ("advice", "advise_speed", acting_kit.PUMP, 0.9, "healthy", 4),
    ("rejected", "set_valve", acting_kit.VALVE, 500.0, "healthy", 5),
    ("failed", "set_pump", acting_kit.PUMP, False, "healthy", 6),
    ("blocked", "set_pump", acting_kit.PUMP, True, "failed", 7),
    ("approved", "set_valve", acting_kit.VALVE, 30.0, "healthy", 8),
    ("mismatch", "set_valve", acting_kit.VALVE, 25.0, "healthy", 9),
    ("turned_down", "set_valve", acting_kit.VALVE, 40.0, "healthy", 10),
)
LOOP_STEP = 16
RECONCILE_STEP = 15


@dataclass
class ActingWorld:
    served: Served
    plant: acting_kit.Plant
    records: dict[str, str]
    loop_run: dict[str, Any]


def acting_document(text: str) -> dict[str, Any]:
    document = acting_kit.manifest_document(text)
    document["contract"]["notification"] = {"kind": "log"}
    document["contract"]["questions"] = {
        "setting_ahead": {
            "text": "Where will the valve's setting be in half an hour?",
            "kind": "predictive",
            "priority": 1,
            "tolerance": {"metric": "mae", "value": 0.5},
            "staleness": 3600,
            "targets": ["Valve.setting"],
            "horizon": 1800,
            "model": "setting",
        }
    }
    document["models"] = {
        "setting": {
            "question": "setting_ahead",
            "target": "Valve.setting",
            "candidates": {"naive": {"family": "persistence"}},
            "backtest": {"initial_window": 3600, "step": 900},
            "acceptance": {"metric": "mae", "threshold": 0.5},
        }
    }
    document["fidelity"] = {
        "interval": "PT15M",
        "window": int((6 * acting_kit.STEP).total_seconds()),
        "correction": {"retry_budget": 1},
    }
    return document


def pump_policy(function: str, *, deadline_s: float) -> dict[str, Any]:
    return {
        "actions": [
            {
                "action": "set_pump",
                "object": acting_kit.PUMP,
                "params": {"value": True},
                "reason": "the tank is low",
            },
            {
                "action": "set_valve",
                "object": acting_kit.VALVE,
                "params": {"value": 15.0},
                "reason": "more flow",
            },
        ],
        "notes": ["the valve stands at 11"],
    }


def build_acting(settings: StoreSettings, water_text: str, root: Path, logs: Path) -> ActingWorld:
    text = acting_kit.ontology_text(water_text)
    manifest = acting_kit.manifest_of(acting_document(text))
    twin = install_twin(
        settings, Bundle(manifest=manifest, ontology_text=text), t=acting_kit.T0
    ).twin
    acting_kit.observe(
        twin,
        acting_kit.at(1),
        {
            "Junction": [
                {"uid": acting_kit.J1, "name": "J1", "elevation": 40.0},
                {"uid": acting_kit.J2, "name": "J2", "elevation": 38.0},
            ],
            "Pump": [acting_kit.link(acting_kit.PUMP, running=False, speed=1.0)],
            "Valve": [
                acting_kit.link(acting_kit.VALVE, valve_type="tcv", setting=5.0),
                acting_kit.link(acting_kit.BARE_VALVE, valve_type="tcv"),
            ],
        },
    )
    acting_kit.register(twin, manifest)
    plant_port = free_port()
    plant = acting_kit.Plant(plant_port)
    http = acting_kit.executor(manifest, plant.url)
    made: dict[str, str] = {}
    with contextlib.ExitStack() as cleanup:
        cleanup.callback(http.close)
        for key, action, target, value, state, step in STORY:
            now = acting_kit.at(step)
            plant.mode = "422" if key == "failed" else "ok"
            try:
                record = actions.propose(
                    twin,
                    action,
                    object=target,
                    params={"value": value},
                    reason=f"the test's {key} record",
                    source="operator@plant",
                    state=state,
                    now=now,
                    executor=http,
                )
            except actions.ActionError as refused:
                assert key == "rejected", refused
                record = {"uid": refused.detail["record"]}
            made[key] = record["uid"]
            if key == "approved":
                with contextlib.suppress(acting_kit.Died):
                    actions.approve(
                        twin,
                        record["uid"],
                        by="operator@plant",
                        note="send it",
                        state="healthy",
                        now=now,
                        executor=acting_kit.Dying(http),
                    )
            elif key == "mismatch":
                actions.approve(
                    twin,
                    record["uid"],
                    by="operator@plant",
                    note="looks right",
                    state="healthy",
                    now=now,
                    executor=http,
                )
            elif key == "turned_down":
                actions.reject(twin, record["uid"], by="operator@plant", note="not now", now=now)
        plant.mode = "ok"
        # The valve is seen at 11, not the 25 it was set to; the pump runs as it was set.
        acting_kit.observe(
            twin,
            acting_kit.at(RECONCILE_STEP - 1),
            {
                "Valve": [{"uid": acting_kit.VALVE, "setting": 11.0}],
                "Pump": [{"uid": acting_kit.PUMP, "running": True}],
            },
        )
        actions.reconcile(twin, now=acting_kit.at(RECONCILE_STEP))
        actions.enable(twin, "pumping", by="operator@plant", now=acting_kit.at(LOOP_STEP))
        loop_run = actions.run_loop(
            twin,
            "pumping",
            policy=pump_policy,
            state=str(fidelity.state(twin)["state"]),
            now=acting_kit.at(LOOP_STEP),
            executor=http,
            key=f"loop:pumping:{acting_kit.at(LOOP_STEP).isoformat()}",
        )
    # A thousand more records, proposed before the story began.
    template = twin.store.get(made["proposed"]).values
    copies = [
        {
            **template,
            "uid": f"twin_action_record:bulk{number:04d}",
            "proposed_at": acting_kit.T0 - timedelta(minutes=number + 1),
        }
        for number in range(BULK_RECORDS)
    ]
    acting_kit.register_rows(twin, {"TwinActionRecord": copies})
    clock = FixedClock(acting_kit.at(LOOP_STEP))
    env = {"PLANT_URL": plant.url, "PLANT_TOKEN": PLANT_TOKEN}
    served = serve(twin, clock, root, logs, env=env)

    def stop_plant() -> None:
        plant.stop()
        # The range of test ports is small: a stopped plant gives its port back.
        release_port(plant_port)

    served.stops.append(stop_plant)
    return ActingWorld(served=served, plant=plant, records=made, loop_run=loop_run)


@contextlib.contextmanager
def closing(served: Served) -> Iterator[Served]:
    try:
        yield served
    finally:
        served.close()
