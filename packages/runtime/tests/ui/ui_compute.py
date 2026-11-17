"""A computed twin for the interface's tests: its questions answered by functions and
models, its scenarios run through a real worker, served beside its interface.

The water network of the API's compute kit, widened: TANKS tanks and as many junctions,
each tank fed by its own valve from its own junction (`V<i>: J<i> -> T<i>`). Version i
is written by a sync at step i (15 minutes each) after T0, for i = 1..LAST:

    T<k> level    4.0 + 0.1 k + CYCLE[i % 4]      J<k> demand   10.0 + k + i

The model `whatif/plant` (the twin's own interventional family) fills each tank by INFLOW
and drains it by OUTFLOW per step; a closed valve stops the inflow of its tank.

Questions, by priority: level_ahead (forecast by the model `level`), levels (latest, left
unmeasured: provisional), mean_level (one number), level_history (a series, `from` and
`to` required), demand_ahead (its model never accepted: provisional, and refused),
level_whatif (no function), slow (gives up at its budget: failing), stuck (raises:
failing).

Scenarios, all of one base (the head once the questions are measured): `closed` closes
the valves V1..V14, each one step later than the one before, over 24 steps; `as_is`
assumes nothing over the same horizon; both are run. `not_run` is made and left;
`dropped` is made and deleted; `failed` is run with too short a budget.

A plain twin of the same manifest has no worker, no model evaluated and no scenario.
"""

from __future__ import annotations

import contextlib
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from compute_testkit import (
    BALANCE,
    CYCLE,
    PLANT_MODULE,
    SEASON,
    T0,
    at,
    functions,
    manifest_for,
    models,
    plant_code,
    questions,
)
from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance, StoreSettings, VersionRef
from dta_runtime.api import create_app as create_api
from dta_runtime.clock import FixedClock
from dta_runtime.settings import RuntimeSettings, UiSettings, WorkerSettings
from dta_runtime.twin import Bundle, Twin, install_twin
from dta_runtime.ui import create_app as create_ui
from dta_runtime.worker import create_app as create_worker
from live_api_testkit import wait_for_job
from ui_testkit import DRIVER_AGENT, ApiServer, RequestLog, ResponseLog, UiServer, free_port

TANKS = 20
LAST = 48
PLAIN_LAST = 8
INFLOW, OUTFLOW = 0.5, 0.05
HORIZON_STEPS = 24
CLOSED_VALVES = 14
JOB_S = 300.0
V1 = "/api/v1"
PLANT = "twin_model:whatif/plant"
SLEEP_S = 3

Row = dict[str, Any]


def iso(steps: float) -> str:
    return at(steps).isoformat().replace("+00:00", "Z")


def tank_uid(k: int) -> str:
    return f"node:T{k}"


def junction_uid(k: int) -> str:
    return f"node:J{k}"


def valve_uid(k: int) -> str:
    return f"link:V{k}"


def level(k: int, step: int) -> float:
    return round(4.0 + 0.1 * k + CYCLE[step % 4], 6)


def demand(k: int, step: int) -> float:
    return 10.0 + k + step


def ui_questions() -> dict[str, Any]:
    kit = questions()

    def tolerance(value: float, unit: str | None = "m") -> dict[str, Any]:
        return {"metric": "mae", "value": value, "unit": unit}

    return {
        "level_ahead": {**kit["level_ahead"], "priority": 1},
        "levels": {**kit["levels"], "priority": 2},
        "mean_level": {
            "text": "How full are the tanks on average?",
            "kind": "descriptive",
            "function": "mean_level",
            "targets": ["Tank.level"],
            "tolerance": tolerance(0.5),
            "priority": 3,
        },
        "level_history": {
            "text": "How full was each tank, version by version?",
            "kind": "descriptive",
            "function": "level_series",
            "targets": ["Tank.level"],
            "tolerance": tolerance(0.5),
            "example": {"from": "#1", "to": "head"},
            "priority": 4,
        },
        "demand_ahead": {**kit["demand_ahead"], "priority": 5},
        "level_whatif": {**kit["level_whatif"], "priority": 6},
        "slow": {
            "text": "Is the plant's slow check passing?",
            "kind": "diagnostic",
            "function": "slow",
            "tolerance": tolerance(1.0, None),
            "priority": 7,
        },
        "stuck": {
            "text": "Is any valve stuck?",
            "kind": "diagnostic",
            "function": "stuck",
            "tolerance": tolerance(1.0, None),
            "priority": 8,
        },
    }


def ui_functions() -> dict[str, Any]:
    kit = functions()
    return {
        "tank_levels": kit["tank_levels"],
        "level_forecast": kit["level_forecast"],
        "demand_forecast": kit["demand_forecast"],
        "mean_level": {
            "kind": "builtin",
            "entry": "aggregate",
            "params": {"property": "Tank.level", "op": "mean"},
            "latency_budget": "PT5S",
        },
        "level_series": {
            "kind": "builtin",
            "entry": "series",
            "params": {"property": "Tank.level"},
            "latency_budget": "PT10S",
        },
        "slow": {
            "kind": "registered",
            "entry": f"{PLANT_MODULE}:sleepy",
            "params": {"s": SLEEP_S},
            "latency_budget": "PT1S",
        },
        "stuck": kit["stuck"],
    }


def ui_models() -> dict[str, Any]:
    found = models({"naive": {"family": "seasonal_naive", "params": SEASON}})
    feeds = {valve_uid(k): tank_uid(k) for k in range(1, TANKS + 1)}
    found["whatif"]["candidates"] = {
        "plant": {
            "family": BALANCE,
            "params": {"inflow": INFLOW, "outflow": OUTFLOW, "feeds": feeds},
        }
    }
    return found


def ui_manifest(ontology_text: str) -> Any:
    contract = {
        "tier": "live",
        "questions": ui_questions(),
        "boundary": {
            "state": ["Tank.level"],
            "controls": ["Valve.status"],
            "observations": ["Tank.level", "Junction.demand"],
        },
    }
    return manifest_for(
        ontology_text, contract=contract, functions=ui_functions(), models=ui_models()
    )


def _tank(k: int, value: float) -> Row:
    return {
        "uid": tank_uid(k),
        "name": f"Tank T{k}",
        "elevation": 60.0,
        "min_level": 0.0,
        "max_level": 20.0,
        "level": value,
    }


def _junction(k: int, value: float) -> Row:
    return {
        "uid": junction_uid(k),
        "name": f"Junction J{k}",
        "elevation": 40.0,
        "base_demand": 5.0,
        "demand": value,
    }


def _valve(k: int) -> Row:
    return {
        "uid": valve_uid(k),
        "start_node": junction_uid(k),
        "end_node": tank_uid(k),
        "valve_type": "prv",
        "status": "open",
        "setting": 1.0,
    }


def write_history(twin: Twin, last: int) -> dict[int, VersionRef]:
    """The network and its history; the version written at each step."""
    versions: dict[int, VersionRef] = {}
    keys = range(1, TANKS + 1)
    for step in range(1, last + 1):
        upserts: dict[str, list[Row]] = {
            "Tank": [_tank(k, level(k, step)) for k in keys],
            "Junction": [_junction(k, demand(k, step)) for k in keys],
        }
        if step == 1:
            upserts["Valve"] = [_valve(k) for k in keys]
        t = at(step)
        changes = ChangeSet(
            upserts=upserts,
            provenance=Provenance(source="scada", kind=FactKind.observed, t_valid=t),
        )
        versions[step] = twin.store.commit(changes, CommitMeta(cause="sync", actor="scada", t=t))
    return versions


def closed(k: int, start: float) -> dict[str, Any]:
    return {"target": "Valve.status", "uid": valve_uid(k), "value": "closed", "start": iso(start)}


SCENARIOS: dict[str, dict[str, Any]] = {
    "closed": {
        "name": "valves closed",
        "description": "V1 to V14 shut one after another",
        "interventions": [closed(k, LAST + k) for k in range(1, CLOSED_VALVES + 1)],
        "horizon": "PT6H",
        "models": [PLANT],
    },
    "as_is": {"name": "as it is", "horizon": "PT6H", "models": [PLANT]},
    "not_run": {
        "name": "not run yet",
        "interventions": [closed(TANKS, LAST + 1)],
        "horizon": "PT1H",
        "models": [PLANT],
    },
    "dropped": {
        "name": "dropped",
        "interventions": [closed(TANKS - 1, LAST + 2)],
        "horizon": "PT1H",
        "models": [PLANT],
    },
    "failed": {
        "name": "out of time",
        "interventions": [closed(TANKS - 2, LAST + 1)],
        "horizon": "PT1H",
        "models": [PLANT],
    },
}


@dataclass
class Computed:
    """A computed twin behind its API, its interface beside it; the test's own calls to
    the API carry DRIVER_AGENT."""

    twin: Twin
    token: str
    api_url: str
    ui_url: str
    api_requests: RequestLog
    ui_responses: ResponseLog
    log_file: Path
    driver: httpx.Client
    versions: dict[int, VersionRef] = field(default_factory=dict)
    base: VersionRef | None = None
    # The scenarios made, by their key in SCENARIOS, as the API last described them.
    scenarios: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        response = self.driver.get(V1 + path, params=params or None)
        assert response.status_code == 200, response.text
        return response.json()

    def post(self, path: str, json: Any = None, **params: Any) -> httpx.Response:
        return self.driver.post(V1 + path, json=json, params=params or None)

    def done(self, response: httpx.Response) -> dict[str, Any]:
        """The finished job a 202 answer names."""
        assert response.status_code in (200, 202), response.text
        job_id = response.json()["job"]["id"]
        return wait_for_job(lambda: self.get(f"/lifecycle/jobs/{job_id}")["data"], JOB_S)

    def compare(self, key: str, targets: list[str], against: str = "main") -> list[Row]:
        """Every row of a comparison, as the API gives it."""
        found: list[Row] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"targets": ",".join(targets), "against": against}
            params |= {"limit": 10_000} | ({"cursor": cursor} if cursor else {})
            page = self.get(f"/scenarios/{self.scenarios[key]['id']}/compare", **params)
            found += page["data"]
            cursor = page.get("next_cursor")
            if not cursor:
                return found


def _make_scenario(world: Computed, key: str) -> dict[str, Any]:
    assert world.base is not None
    made = world.post("/scenarios", json=SCENARIOS[key], at=world.base.id)
    assert made.status_code == 201, made.text
    return made.json()["data"]


def build_scenarios(world: Computed) -> None:
    for key in ("closed", "as_is", "not_run", "dropped", "failed"):
        world.scenarios[key] = _make_scenario(world, key)
    for key in ("closed", "as_is"):
        job = world.done(world.post(f"/scenarios/{world.scenarios[key]['id']}/run", json={}))
        assert job["state"] == "succeeded", job
    failed = world.done(
        world.post(f"/scenarios/{world.scenarios['failed']['id']}/run", json={"budget_s": 0.001})
    )
    assert failed["state"] == "failed", failed
    gone = world.driver.delete(f"{V1}/scenarios/{world.scenarios['dropped']['id']}")
    assert gone.status_code == 200, gone.text
    for key, scenario in world.scenarios.items():
        world.scenarios[key] = world.get(f"/scenarios/{scenario['id']}")["data"]


@contextlib.contextmanager
def serve_computed(
    store: StoreSettings,
    ontology_text: str,
    root: Path,
    logs_dir: Path,
    *,
    worker: bool = True,
    last: int = LAST,
    prepare: Callable[[Computed], None] | None = None,
) -> Iterator[Computed]:
    """Install the twin, write its history, serve its API (and a worker beside it), and
    its interface; `prepare` is run before the world is handed out."""
    installed = install_twin(
        store, Bundle(manifest=ui_manifest(ontology_text), ontology_text=ontology_text), t=T0
    )
    twin = installed.twin
    token = secrets.token_urlsafe(32)
    with contextlib.ExitStack() as stack:
        stack.callback(twin.close)
        versions = write_history(twin, last)
        api_port = free_port()
        worker_port = free_port() if worker else None
        worker_url = f"http://127.0.0.1:{worker_port}" if worker_port else None
        settings = RuntimeSettings(
            store=store,
            api_token=token,
            artifacts_dir=root / "artifacts",
            logs_dir=logs_dir,
            worker_url=worker_url,
        )
        # The twin's clock stands at its last data.
        api_requests = RequestLog(create_api(settings, twin, clock=FixedClock(at(last))))
        api = stack.enter_context(ApiServer(api_requests, api_port))
        api_url = f"http://127.0.0.1:{api.port}"
        if worker_port is not None:
            served = create_worker(
                WorkerSettings(api_url=api_url, api_token=token, cpus=2.0, memory_mb=2048),
                work_dir=root / "worker",
            )
            stack.enter_context(ApiServer(served, worker_port))
        ui_app = create_ui(UiSettings(api_url=api_url, api_token=token, logs_dir=logs_dir))
        ui_responses = ResponseLog(ui_app.server)
        ui = stack.enter_context(UiServer(ui_responses, free_port()))
        driver = stack.enter_context(
            httpx.Client(
                base_url=api_url,
                headers={"Authorization": f"Bearer {token}", "User-Agent": DRIVER_AGENT},
                timeout=httpx.Timeout(JOB_S),
            )
        )
        world = Computed(
            twin=twin,
            token=token,
            api_url=api_url,
            ui_url=f"http://127.0.0.1:{ui.port}",
            api_requests=api_requests,
            ui_responses=ui_responses,
            log_file=logs_dir / "stack.jsonl",
            driver=driver,
            versions=versions,
        )
        if prepare is not None:
            prepare(world)
        yield world


def register_code(world: Computed) -> None:
    uploaded = world.driver.post(
        f"{V1}/artifacts", content=plant_code(), headers={"Content-Type": "application/zip"}
    )
    assert uploaded.status_code in (200, 201), uploaded.text
    registered = world.post("/lifecycle/register", json={"code": uploaded.json()["sha256"]})
    assert registered.status_code == 200, registered.text


def prepare_computed(world: Computed) -> None:
    """Code registered, the models `level` and `whatif` evaluated, every question but
    `levels` measured, and the scenarios made from the head that leaves."""
    register_code(world)
    for spec in ("level", "whatif"):
        job = world.done(world.post("/models/evaluate", json={"spec": spec}))
        assert job["state"] == "succeeded", job
    measured = [name for name in ui_questions() if name != "levels"]
    job = world.done(world.post("/questions/measure", json={"questions": measured}))
    assert job["state"] == "succeeded", job
    world.base = world.twin.store.head()
    build_scenarios(world)


def prepare_plain(world: Computed) -> None:
    register_code(world)


def horizon_times(steps: int = HORIZON_STEPS) -> list[str]:
    """The grid times of a scenario of `steps` steps from the base, as the API writes them."""
    return [iso(LAST + j) for j in range(steps + 1)]
