"""A water network whose functions, models and questions are computed: its manifest, its
history, the code it brings, and the API served beside a real worker.

The network, on the water ontology:

    V1: J1 -> T1 (open)    V2: J1 -> T2 (open)    P1: J1 -> J2

Version i is written by a sync at step i (15 minutes each) after T0, for i = 1..LAST:

    T1 level   4.0 + CYCLE[i % 4]        T2 level   9.0 + 2 * CYCLE[i % 4]
    J1 demand  10.0 + i                  J2 demand  20.0, never changed

The manifest declares:

    questions   levels (function tank_levels), level_ahead (model level, forecast),
                demand_ahead (model demand, whose history is too short to evaluate),
                level_whatif (model whatif, the twin's own interventional family)
    functions   three built-ins and four of the twin's own code
    models      level: seasonal_naive, theta, ridge_lags and one whose settings cannot be;
                demand: persistence;  whatif: the twin's own balance of in- and outflow
"""

from __future__ import annotations

import contextlib
import io
import threading
import time
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from api_testkit import TOKEN
from dta_core.manifest import Manifest
from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance, StoreSettings, VersionRef
from dta_runtime.api import create_app
from dta_runtime.clock import FixedClock
from dta_runtime.settings import RuntimeSettings, WorkerSettings
from dta_runtime.twin import Bundle, Twin, install_twin
from dta_runtime.worker import create_app as create_worker
from live_api_testkit import HEADERS, free_port, wait_for_job
from runtime_testkit import water_manifest

T0 = datetime(2026, 1, 5, tzinfo=UTC)
STEP = timedelta(minutes=15)
LAST = 48
CYCLE = (0.0, 1.0, 0.5, -0.5)
INFLOW, OUTFLOW = 0.5, 0.2
V1 = "/api/v1"
ARROW = "application/vnd.apache.arrow.stream"
JOB_S = 240.0

Row = dict[str, Any]

PLANT_MODULE = "compute_plant"
# The code the twin brings: a family that models what fills and drains a tank, so that it
# holds under a closed valve the history never showed, and functions over the twin.
PLANT_CODE = '''
import time

import pandas as pd
import pyarrow as pa


class Balance:
    interventional = True

    def __init__(self, step):
        self.step = step

    def fit(self, frame, params, *, horizon):
        return {
            "inflow": float(params["inflow"]),
            "outflow": float(params["outflow"]),
            "feeds": dict(params["feeds"]),
        }

    def predict(self, state, history, horizon, *, levels=(), drivers=None, interventions=()):
        rows = []
        for uid, series in history.groupby("unique_id", sort=True):
            level, moment = float(series["y"].iloc[-1]), series["ds"].iloc[-1]
            valves = [valve for valve, tank in state["feeds"].items() if tank == uid]
            for _ in range(horizon):
                moment = moment + self.step
                closed = any(
                    found.target == "Valve.status"
                    and found.uid in valves
                    and found.value == "closed"
                    and found.start <= moment
                    and (found.end is None or moment < found.end)
                    for found in interventions
                )
                level += (0.0 if closed else state["inflow"]) - state["outflow"]
                rows.append({"unique_id": uid, "ds": moment, "mean": level})
        return pd.DataFrame(rows)

    def validity(self, frame):
        return {"y": (float(frame["y"].min()), float(frame["y"].max()))}


def balance(*, step, twin):
    return Balance(step)


def report(twin, uid):
    found = twin.get(f"/objects/Tank/{uid}")
    return {"level": found["data"]["values"]["level"], "at": twin.at, "branch": twin.branch}


def pressures(twin, factor):
    """A pressure worked out for every junction: its demand times a factor."""
    from dta_core.schema import FactKind
    from dta_runtime.protocol import Derived

    found = twin.get("/objects/Junction", columns="demand")["data"]
    rows = sorted(found, key=lambda row: row["uid"])
    table = pa.table(
        {
            "uid": [row["uid"] for row in rows],
            "pressure": [row["demand"] * factor for row in rows],
        }
    )
    return Derived(type="Junction", rows=table, kind=FactKind.inferred, confidence=0.75)


def broken(twin):
    raise RuntimeError("valve stuck")


def sleepy(twin, s):
    time.sleep(s)
    return s
'''
BALANCE = f"{PLANT_MODULE}:balance"
FEEDS = {"link:V1": "node:T1", "link:V2": "node:T2"}
SEASON = {"season_length": 4}
LAGS = {"lags": [1, 2], "date_features": []}


def at(steps: float) -> datetime:
    return T0 + steps * STEP


def node(key: str) -> str:
    return f"node:{key}"


def link(key: str) -> str:
    return f"link:{key}"


def level(key: str, step: int) -> float:
    return {"T1": 4.0 + CYCLE[step % 4], "T2": 9.0 + 2 * CYCLE[step % 4]}[key]


def demand(key: str, step: int) -> float:
    return {"J1": 10.0 + step, "J2": 20.0}[key]


def plant_code(marker: str = "") -> bytes:
    """The twin's code packed; another `marker` gives another artifact."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as written:
        written.writestr(zipfile.ZipInfo(f"{PLANT_MODULE}.py"), f"# {marker}\n{PLANT_CODE}")
    return buffer.getvalue()


def questions() -> dict[str, Any]:
    def tolerance(value: float, unit: str | None = "m") -> dict[str, Any]:
        return {"metric": "mae", "value": value, "unit": unit}

    return {
        "levels": {
            "text": "How full is each tank?",
            "kind": "descriptive",
            "function": "tank_levels",
            "targets": ["Tank.level"],
            "tolerance": tolerance(0.5),
        },
        "level_ahead": {
            "text": "How full will each tank be in an hour?",
            "kind": "predictive",
            "function": "level_forecast",
            "model": "level",
            "horizon": "PT1H",
            "tolerance": tolerance(0.5),
        },
        "demand_ahead": {
            "text": "What will each junction draw in an hour?",
            "kind": "predictive",
            "function": "demand_forecast",
            "model": "demand",
            "horizon": "PT1H",
            "tolerance": tolerance(5.0, None),
        },
        "level_whatif": {
            "text": "How full would each tank be with a valve closed?",
            "kind": "prescriptive",
            "model": "whatif",
            "horizon": "PT2H",
            "tolerance": tolerance(1000.0),
        },
    }


def functions() -> dict[str, Any]:
    def own(name: str, **more: Any) -> dict[str, Any]:
        return {
            "kind": "registered",
            "entry": f"{PLANT_MODULE}:{name}",
            "latency_budget": "PT30S",
            **more,
        }

    return {
        "tank_levels": {
            "kind": "builtin",
            "entry": "latest",
            "params": {"property": "Tank.level"},
            "latency_budget": "PT5S",
        },
        "level_forecast": {
            "kind": "builtin",
            "entry": "forecast",
            "params": {"question": "level_ahead"},
            "latency_budget": "PT60S",
        },
        "demand_forecast": {
            "kind": "builtin",
            "entry": "forecast",
            "params": {"question": "demand_ahead"},
            "latency_budget": "PT60S",
        },
        "tank_report": own("report"),
        "junction_pressures": own("pressures", params={"factor": 2.0}),
        "stuck": own("broken"),
        "sleepy": own("sleepy"),
    }


def models(level_candidates: Mapping[str, Any] | None = None) -> dict[str, Any]:
    backtest = {"initial_window": "PT4H", "step": "PT1H", "min_folds": 3}
    return {
        "level": {
            "question": "level_ahead",
            "target": "Tank.level",
            "candidates": dict(
                level_candidates
                or {
                    "naive": {"family": "seasonal_naive", "params": SEASON},
                    "theta": {"family": "theta", "params": SEASON},
                    "lags": {"family": "ridge_lags", "params": LAGS},
                    # Settings the family refuses: its backtest fails.
                    "broken": {"family": "ridge_lags", "params": {"lags": []}},
                }
            ),
            "features": [],
            "backtest": backtest,
            "acceptance": {"metric": "mae", "threshold": 0.5, "baseline": "persistence"},
        },
        "demand": {
            "question": "demand_ahead",
            "target": "Junction.demand",
            "candidates": {"last": {"family": "persistence"}},
            "features": [],
            # More history than the twin has: no fold can be laid out.
            "backtest": {"initial_window": "P30D", "step": "PT1H", "min_folds": 3},
            "acceptance": {"metric": "mae", "threshold": 5.0},
        },
        "whatif": {
            "question": "level_whatif",
            "target": "Tank.level",
            "candidates": {
                "plant": {
                    "family": BALANCE,
                    "params": {"inflow": INFLOW, "outflow": OUTFLOW, "feeds": FEEDS},
                }
            },
            "features": [],
            "backtest": backtest,
            "acceptance": {"metric": "mae", "threshold": 1000.0},
            "interventional": True,
        },
    }


def manifest_for(ontology_text: str, revision: int = 1, **sections: Any) -> Manifest:
    document: dict[str, Any] = {
        "twin": {"id": "water", "name": "Water network", "revision": revision},
        "contract": {
            "tier": "live",
            "questions": questions(),
            "boundary": {
                "state": ["Tank.level"],
                "controls": ["Valve.status"],
                "observations": ["Tank.level", "Junction.demand"],
            },
        },
        "sync": {"interval": "PT15M"},
        "functions": functions(),
        "models": models(),
    }
    document.update(sections)
    return water_manifest(ontology_text, **document)


def tank(key: str, value: float | None) -> Row:
    return {
        "uid": node(key),
        "name": f"Tank {key}",
        "elevation": 60.0,
        "min_level": 0.0,
        "max_level": 20.0,
        "level": value,
    }


def junction(key: str, value: float | None) -> Row:
    return {
        "uid": node(key),
        "name": f"Junction {key}",
        "elevation": 40.0,
        "base_demand": 5.0,
        "demand": value,
    }


def valve(key: str, start: str, end: str) -> Row:
    return {
        "uid": link(key),
        "start_node": node(start),
        "end_node": node(end),
        "valve_type": "prv",
        "status": "open",
        "setting": 1.0,
    }


def observe(twin: Twin, t: datetime, upserts: Mapping[str, list[Row]]) -> VersionRef:
    """One version of observed facts on the live line, as a sync writes them."""
    changes = ChangeSet(
        upserts=dict(upserts),
        provenance=Provenance(source="scada", kind=FactKind.observed, t_valid=t),
    )
    return twin.store.commit(changes, CommitMeta(cause="sync", actor="scada", t=t))


def write_history(twin: Twin, last: int = LAST) -> dict[int, VersionRef]:
    """The network and its history; the version written at each step."""
    versions: dict[int, VersionRef] = {}
    for step in range(1, last + 1):
        upserts: dict[str, list[Row]] = {
            "Tank": [tank("T1", level("T1", step)), tank("T2", level("T2", step))],
            "Junction": [junction("J1", demand("J1", step))],
        }
        if step == 1:
            upserts["Junction"].append(junction("J2", demand("J2", step)))
            upserts["Valve"] = [valve("V1", "J1", "T1"), valve("V2", "J1", "T2")]
            upserts["Pipe"] = [
                {
                    "uid": link("P1"),
                    "start_node": node("J1"),
                    "end_node": node("J2"),
                    "status": "open",
                    "length": 100.0,
                    "diameter": 0.3,
                }
            ]
        versions[step] = observe(twin, at(step), upserts)
    return versions


@contextlib.contextmanager
def serve_on(app: Any, port: int) -> Iterator[str]:
    """The application served by uvicorn in a thread, on a port chosen beforehand: the
    API and its worker each have to be told where the other is before either starts."""
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_config=None, access_log=False, lifespan="on"
        )
    )
    thread = threading.Thread(target=server.run, name=f"served-{port}", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30.0
    pause = threading.Event()
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the server did not start"
        pause.wait(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=60.0)


@dataclass
class Calls:
    """Requests to an API under `/api/v1`, with the token; and the jobs they start."""

    client: Any

    def get(self, path: str, **params: Any) -> Any:
        return self.client.get(V1 + path, params=params or None, headers=HEADERS)

    def post(self, path: str, json: Any = None, **options: Any) -> Any:
        headers = {**HEADERS, **(options.pop("headers", None) or {})}
        return self.client.post(V1 + path, json=json, headers=headers, **options)

    def delete(self, path: str) -> Any:
        return self.client.delete(V1 + path, headers=HEADERS)

    def ok(self, path: str, **params: Any) -> dict[str, Any]:
        response = self.get(path, **params)
        assert response.status_code == 200, response.text
        return response.json()

    def refused(self, response: Any, status: int, code: str) -> dict[str, Any]:
        assert response.status_code == status, response.text
        assert response.headers["content-type"].startswith("application/problem+json")
        body = response.json()
        assert body["code"] == code, body
        return body

    def wait(self, response: Any, timeout_s: float = JOB_S) -> dict[str, Any]:
        """The finished job a 202 or 200 answer names."""
        assert response.status_code in (200, 202), response.text
        job_id = response.json()["job"]["id"]
        assert response.headers["location"] == f"{V1}/lifecycle/jobs/{job_id}"
        return wait_for_job(lambda: self.ok(f"/lifecycle/jobs/{job_id}")["data"], timeout_s)

    def done(self, response: Any, timeout_s: float = JOB_S) -> dict[str, Any]:
        """The result of the job a response names, which has to succeed."""
        job = self.wait(response, timeout_s)
        assert job["state"] == "succeeded", job
        return job["result"]

    def upload(self, content: bytes) -> str:
        response = self.client.post(
            f"{V1}/artifacts",
            content=content,
            headers={**HEADERS, "Content-Type": "application/zip"},
        )
        assert response.status_code in (200, 201), response.text
        return response.json()["sha256"]

    def register(self, code: str | None = None) -> dict[str, Any]:
        response = self.post("/lifecycle/register", json={"code": code})
        assert response.status_code == 200, response.text
        return response.json()


@dataclass
class Reached(Calls):
    """An API and, beside it, the twin it serves, for what a test reads past the API."""

    twin: Twin = None  # type: ignore[assignment]
    app: Any = None

    @property
    def services(self) -> Any:
        return self.app.state.services

    @property
    def head(self) -> VersionRef:
        return self.twin.store.head()

    def store_rows(self, type_name: str, branch: str = "main") -> dict[str, Row]:
        store = self.twin.store
        rows = store.table(type_name, branch=branch, ontology=self.twin.ontology).to_pylist()
        return {row["uid"]: row for row in rows}


@dataclass
class World(Reached):
    """The computed twin behind its API, served over HTTP, and the worker beside it."""

    settings: RuntimeSettings = None  # type: ignore[assignment]
    url: str = ""
    worker_url: str = ""
    worker_app: Any = None
    versions: dict[int, VersionRef] = field(default_factory=dict)
    # The hash of the code the twin's functions and its own family are found in.
    code: str = ""


@dataclass
class Small(Reached):
    """A computed twin of a test's own behind an API called in process."""

    ontology_text: str = ""

    def revise(self, revision: int, **sections: Any) -> None:
        """Record another revision of the manifest, as installing the bundle again does."""
        manifest = manifest_for(self.ontology_text, revision, **sections)
        again = install_twin(
            self.twin.store.settings, Bundle(manifest=manifest, ontology_text=self.ontology_text)
        )
        assert again.actions == ("registered",), again.actions
        again.twin.close()


def runtime_settings(store: StoreSettings, root: Path, worker_url: str | None) -> RuntimeSettings:
    return RuntimeSettings(
        store=store,
        api_token=TOKEN,
        artifacts_dir=root / "artifacts",
        logs_dir=root / "logs",
        worker_url=worker_url,
    )


@contextlib.contextmanager
def serve_world(
    store: StoreSettings,
    ontology_text: str,
    root: Path,
    *,
    cpus: float = 2.0,
    memory_mb: int = 2048,
) -> Iterator[World]:
    """Install the twin, write its history, and serve its API and a worker for it."""
    installed = install_twin(
        store, Bundle(manifest=manifest_for(ontology_text), ontology_text=ontology_text), t=T0
    )
    twin = installed.twin
    with contextlib.ExitStack() as stack:
        stack.callback(twin.close)
        versions = write_history(twin)
        api_port, worker_port = free_port(), free_port()
        worker_url = f"http://127.0.0.1:{worker_port}"
        settings = runtime_settings(store, root, worker_url)
        # The twin's clock stands at its last data, so nothing here is as late as today.
        app = create_app(settings, twin, clock=FixedClock(at(LAST)))
        url = stack.enter_context(serve_on(app, api_port))
        worker = create_worker(
            WorkerSettings(api_url=url, api_token=TOKEN, cpus=cpus, memory_mb=memory_mb),
            work_dir=root / "worker",
        )
        stack.enter_context(serve_on(worker, worker_port))
        client = stack.enter_context(httpx.Client(base_url=url, timeout=httpx.Timeout(JOB_S)))
        yield World(
            client=client,
            twin=twin,
            settings=settings,
            url=url,
            worker_url=worker_url,
            app=app,
            worker_app=worker,
            versions=versions,
        )
