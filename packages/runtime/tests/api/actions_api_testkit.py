"""A water twin that acts on a plant through its API, with a real worker beside it, a
plant that answers its calls over HTTP and, when asked, a webhook its notices go to.

The network and its controls are those of the action library's tests: the lake pump
PMP-10 (`set_pump`, autonomous), the valve VLV-1 at 5.0 (`set_valve`, supervised, 1 to
200, `max_step` 50, settle PT45M, tolerance 0.5) and the pump's speed (`advise_speed`,
advisory). Values are observed at step 1 (15 minutes each after T0).

Besides, the manifest declares:

    question   setting_ahead   the valve's setting half an hour ahead (model `setting`,
                               persistence, never evaluated here): its freshness is what
                               a check is told, so a stale check fails the twin
    fidelity   checked every 15 minutes, retry_budget 1
    functions  pump_policy, broken_policy, bad_policy (the loops' policies);
               junction_pressures and stuck_gauge (on_commit; the second raises);
               quarter (interval, every 15 minutes)
    loops      pumping (pump and valve; pump_policy), broken (broken_policy),
               garbled (bad_policy)
    source     telemetry: the pump's speed from a folder of CSV files

The twin's fidelity state is moved by the fidelity library, as the checks and the
incidents of the API would move it: `healthy` after a check told the data is fresh,
`failed` after one told it is stale, `correcting` while a reconcile incident is open.
"""

from __future__ import annotations

import contextlib
import io
import random
import socket
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from actions_testkit import (
    BARE_VALVE,
    J1,
    J2,
    PUMP,
    STEP,
    T0,
    VALVE,
    Plant,
    at,
    link,
    manifest_document,
    manifest_of,
    observe,
    ontology_text,
)
from actions_testkit import TOKEN as PLANT_TOKEN
from api_testkit import TOKEN
from compute_testkit import JOB_S, Calls, runtime_settings, serve_on
from dta_core.errors import DtaError
from dta_core.store import StoreSettings, VersionRef
from dta_runtime import fidelity
from dta_runtime.api import create_app
from dta_runtime.fidelity import Freshness
from dta_runtime.settings import WorkerSettings
from dta_runtime.twin import Bundle, Twin, install_twin
from dta_runtime.worker import create_app as create_worker
from fidelity_api_testkit import Clock
from live_testkit import Folder, speeds_source

# Names of the action library's tests that the tests of the API use as they are.
__all__ = ["BARE_VALVE", "PLANT_TOKEN", "PUMP", "T0", "VALVE"]

# The ports the tests of the API's actions listen on.
PORTS = range(18980, 19000)
WEBHOOK_ENV = "DTA_TEST_ACTIONS_WEBHOOK"
QUESTION = "setting_ahead"
CODE_MODULE = "plant"
APPROVAL_PENDING = "twin.action.approval_pending"
BLOCKED = "twin.action.blocked"
MISMATCH = "twin.action.mismatch"
INCIDENT = "twin.fidelity.incident"
PLANT_CALLS = "/api/v1/controls/"

CODE = '''
import pyarrow as pa


def policy(twin):
    """Run the pump, and open the valve ten more than it stands."""
    setting = float(twin.get("/objects/Valve/link:VLV-1")["data"]["values"]["setting"])
    return {
        "actions": [
            {
                "action": "set_pump",
                "object": "link:PMP-10",
                "params": {"value": True},
                "reason": "the tank is low",
            },
            {
                "action": "set_valve",
                "object": "link:VLV-1",
                "params": {"value": setting + 10.0},
                "reason": "more flow",
            },
        ],
        "notes": [f"the valve stands at {setting:g}"],
    }


def broken_policy(twin):
    raise RuntimeError("the policy cannot decide")


def bad_policy(twin):
    return {"actions": [{"action": "set_pump", "object": "link:PMP-10"}], "extra": 1}


def pressures(twin):
    """A pressure for every junction, a tenth of its elevation."""
    from dta_core.schema import FactKind
    from dta_runtime.protocol import Derived

    found = sorted(twin.get("/objects/Junction")["data"], key=lambda row: row["uid"])
    table = pa.table(
        {
            "uid": [row["uid"] for row in found],
            "pressure": [float(row["elevation"]) * 0.1 for row in found],
        }
    )
    return Derived(type="Junction", rows=table, kind=FactKind.inferred, confidence=0.5)


def stuck(twin):
    raise RuntimeError("the gauge is stuck")


def quarter(twin):
    return 4
'''


def own_port(taken: set[int] | None = None) -> int:
    """A port of the range that nothing listens on now. Servers here set SO_REUSEADDR, so
    a port a closed server left in TIME_WAIT counts as free."""
    candidates = list(PORTS)
    random.shuffle(candidates)
    for port in candidates:
        if taken and port in taken:
            continue
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port in {PORTS.start}..{PORTS.stop - 1}")


def code() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as written:
        written.writestr(zipfile.ZipInfo(f"{CODE_MODULE}.py"), CODE)
    return buffer.getvalue()


def _own(name: str, **more: Any) -> dict[str, Any]:
    return {
        "kind": "registered",
        "entry": f"{CODE_MODULE}:{name}",
        "complexity": "O(1)",
        "latency_budget": "PT30S",
        **more,
    }


def acting_document(
    text: str,
    telemetry: Folder,
    *,
    webhook: bool,
    executors: bool = True,
    fidelity_section: bool = True,
) -> dict[str, Any]:
    document = manifest_document(text)
    contract = document["contract"]
    contract["addons"] = {"executors": executors}
    contract["notification"] = (
        {"kind": "webhook", "webhook_env": WEBHOOK_ENV} if webhook else {"kind": "log"}
    )
    contract["boundary"]["observations"] = ["Pump.running", "Valve.setting", "Pump.speed"]
    contract["questions"] = {
        QUESTION: {
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
            "question": QUESTION,
            "target": "Valve.setting",
            "candidates": {"naive": {"family": "persistence"}},
            "backtest": {"initial_window": 3600, "step": 900},
            "acceptance": {"metric": "mae", "threshold": 0.5},
        }
    }
    if fidelity_section:
        document["fidelity"] = {
            "interval": "PT15M",
            "window": int((6 * STEP).total_seconds()),
            "correction": {"retry_budget": 1},
        }
    document["functions"] = {
        "pump_policy": _own("policy"),
        "broken_policy": _own("broken_policy"),
        "bad_policy": _own("bad_policy"),
        "junction_pressures": _own("pressures", trigger="on_commit"),
        "stuck_gauge": _own("stuck", trigger="on_commit"),
        "quarter": _own("quarter", trigger="interval", interval="PT15M"),
    }
    document["control_loops"] = {
        "pumping": {
            "description": "Keeps the pump and the valve where the policy wants them",
            "controls": ["pump_running", "valve_setting"],
            "policy": "pump_policy",
            "interval": "PT15M",
        },
        "broken": {"controls": ["pump_running"], "policy": "broken_policy", "interval": "PT15M"},
        "garbled": {"controls": ["pump_running"], "policy": "bad_policy", "interval": "PT1H"},
    }
    document["sources"] = {"telemetry": speeds_source(telemetry)}
    return document


def no_predictor(model: Mapping[str, Any], **_: Any) -> Any:
    """No model is evaluated in these tests: a check never asks for a forecast."""
    raise DtaError("no model is fitted in these tests", code="worker.unavailable")


@dataclass
class Acting(Calls):
    """The twin behind its API and worker, the plant it acts on and its webhook."""

    twin: Twin = None  # type: ignore[assignment]
    app: Any = None
    clock: Clock = None  # type: ignore[assignment]
    plant: Plant = None  # type: ignore[assignment]
    receiver: Any = None
    telemetry: Folder = None  # type: ignore[assignment]
    url: str = ""
    registered: dict[str, Any] = field(default_factory=dict)

    @property
    def services(self) -> Any:
        return self.app.state.services

    @property
    def head(self) -> VersionRef:
        return self.twin.store.head()

    # -- the twin's state, as the fidelity library moves it ----------------------------

    def check(self, step: float, fresh: bool | None = True) -> str:
        """A fidelity check at a step, told whether the valve's data is fresh; the state."""
        freshness = (
            {}
            if fresh is None
            else {
                QUESTION: Freshness(
                    fresh=fresh,
                    age_s=900.0 if fresh else 7200.0,
                    staleness_s=3600.0,
                    source="scada",
                )
            }
        )
        fidelity.check(
            self.twin,
            at(step),
            predictor=no_predictor,
            freshness=freshness,
            trace_id="actions-api",
            deadline_s=30.0,
        )
        return self.state()

    def correcting(self, step: float, subject: str = "twin_action_record:elsewhere") -> str:
        """An open reconcile incident on a record of no action: the twin is correcting."""
        fidelity.open_incident(self.twin, "reconcile", subject, {"why": "test"}, now=at(step))
        return self.state()

    def state(self) -> str:
        return str(fidelity.state(self.twin)["state"])

    def into(self, state: str, step: float) -> None:
        """Bring a twin that was never checked into `state` at a step."""
        if state == "unknown":
            assert self.state() == "unknown"
        elif state == "healthy":
            assert self.check(step) == "healthy"
        elif state == "correcting":
            assert self.correcting(step) == "correcting"
        elif state == "failed":
            assert self.check(step, fresh=False) == "failed"
        else:
            raise AssertionError(state)
        self.clock.set(at(step))

    # -- requests ----------------------------------------------------------------------

    def propose(
        self,
        action: str,
        object_uid: str,
        value: Any = None,
        *,
        params: Mapping[str, Any] | None = None,
        key: str | None = None,
        source: str = "operator@plant",
        reason: str | None = "the test asks",
    ) -> Any:
        body = {
            "object": object_uid,
            "params": dict(params) if params is not None else {"value": value},
            "reason": reason,
            "source": source,
        }
        headers = {"Idempotency-Key": key} if key else {}
        return self.post(f"/actions/{action}/propose", json=body, headers=headers)

    def proposed(self, action: str, object_uid: str, value: Any = None, **options: Any) -> dict:
        response = self.propose(action, object_uid, value, **options)
        assert response.status_code == 201, response.text
        return response.json()["data"]

    def decide(self, uid: str, step: str, by: str = "operator@plant", note: str | None = None):
        return self.post(f"/actions/records/{uid}/{step}", json={"by": by, "note": note})

    def record(self, uid: str) -> dict[str, Any]:
        return self.ok(f"/actions/records/{uid}")["data"]

    def calls(self) -> list[Any]:
        """What the plant was sent."""
        return [found for found in self.plant.received if found.path.startswith(PLANT_CALLS)]

    def events(self, *types: str) -> list[dict[str, Any]]:
        found = self.receiver.events() if self.receiver is not None else []
        return [event for event in found if not types or event["type"] in types]

    def wait_events(self, count: int, *types: str, timeout_s: float = 30.0) -> list[dict]:
        deadline = time.monotonic() + timeout_s
        while True:
            found = self.events(*types)
            if len(found) >= count:
                return found
            assert time.monotonic() < deadline, f"{len(found)} of {count} events: {found}"
            time.sleep(0.02)

    def job(self, job_id: str, timeout_s: float = JOB_S) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            found = self.ok(f"/lifecycle/jobs/{job_id}")["data"]
            if found["state"] in ("succeeded", "failed"):
                return found
            assert time.monotonic() < deadline, f"job {job_id} still {found['state']}"
            time.sleep(0.05)

    def store_rows(self, type_name: str) -> dict[str, dict[str, Any]]:
        rows = self.twin.store.table(type_name, ontology=self.twin.ontology).to_pylist()
        return {row["uid"]: row for row in rows}


def world_rows() -> dict[str, list[dict[str, Any]]]:
    return {
        "Junction": [
            {"uid": J1, "name": "J1", "elevation": 40.0},
            {"uid": J2, "name": "J2", "elevation": 38.0},
        ],
        "Pump": [link(PUMP, running=False, speed=1.0)],
        "Valve": [
            link(VALVE, valve_type="tcv", setting=5.0),
            link(BARE_VALVE, valve_type="tcv"),
        ],
    }


@contextlib.contextmanager
def serve_acting(
    store: StoreSettings,
    water_text: str,
    root: Path,
    setenv: Callable[[str, str], None],
    *,
    webhook_receiver: Any = None,
    executors: bool = True,
    fidelity_section: bool = True,
    worker: bool = True,
    register: bool = True,
) -> Iterator[Acting]:
    """Install the twin, observe its network at step 1, and serve its API, a worker and
    the plant; the code is kept and the registries brought in line with the manifest."""
    text = ontology_text(water_text)
    telemetry = Folder(root / "telemetry")
    document = acting_document(
        text,
        telemetry,
        webhook=webhook_receiver is not None,
        executors=executors,
        fidelity_section=fidelity_section,
    )
    twin = install_twin(
        store, Bundle(manifest=manifest_of(document), ontology_text=text), t=T0
    ).twin
    observe(twin, at(1), world_rows())
    taken = {webhook_receiver.port} if webhook_receiver is not None else set()
    api_port = own_port(taken)
    worker_port = own_port(taken | {api_port})
    plant_port = own_port(taken | {api_port, worker_port})
    with contextlib.ExitStack() as stack:
        stack.callback(twin.close)
        plant = Plant(plant_port)
        stack.callback(plant.stop)
        setenv("PLANT_URL", plant.url)
        setenv("PLANT_TOKEN", PLANT_TOKEN)
        if webhook_receiver is not None:
            setenv(WEBHOOK_ENV, webhook_receiver.url)
        clock = Clock(at(1))
        worker_url = f"http://127.0.0.1:{worker_port}"
        settings = runtime_settings(store, root, worker_url)
        app = create_app(settings, twin, clock=clock, notify_backoff_s=(0.05, 0.1))
        url = stack.enter_context(serve_on(app, api_port))
        if worker:
            served = create_worker(
                WorkerSettings(api_url=url, api_token=TOKEN, cpus=1.0, memory_mb=2048),
                work_dir=root / "worker",
            )
            stack.enter_context(serve_on(served, worker_port))
        client = stack.enter_context(httpx.Client(base_url=url, timeout=httpx.Timeout(JOB_S)))
        world = Acting(
            client=client,
            twin=twin,
            app=app,
            clock=clock,
            plant=plant,
            receiver=webhook_receiver,
            telemetry=telemetry,
            url=url,
        )
        if register:
            world.registered = world.register(world.upload(code()))
        yield world
