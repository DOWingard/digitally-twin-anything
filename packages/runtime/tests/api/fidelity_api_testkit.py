"""A live twin whose fidelity is checked through its API, with a real worker beside it.

Two tanks are observed at every step (15 minutes): T1 at 5.0 and T2 at 8.0 until the
models are evaluated at step BUILT; after it, both plus whatever the test's `drift`
says for the step. Both move alike, so the series are never told apart as biased.

The question `level_ahead` asks for the level two steps ahead within 0.5 (MAE). Its
specification `level` has one candidate, `hold`, a family of the twin's own that
predicts the last value it was fitted to: a change after its fit stays an error until
it is fitted again, and fitting again on data after the change learns a level shift.
The fidelity window is six steps, so a correction's recent evaluation backtests five
origins.

A step writes the step's data, moves the clock to it and asks for the check of that
boundary under the scheduler's key, then waits for the corrections the check queued:
the twin is driven only by asking for checks, as the scheduler would.
"""

from __future__ import annotations

import contextlib
import io
import threading
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from compute_testkit import (
    Calls,
    manifest_for,
    observe,
    runtime_settings,
    serve_on,
    tank,
)
from dta_core.store import StoreSettings, VersionRef
from dta_runtime.api import create_app
from dta_runtime.api.jobs import Work
from dta_runtime.clock import ClockUnavailable, FixedClock
from dta_runtime.settings import RuntimeSettings, WorkerSettings
from dta_runtime.twin import Bundle, Twin, install_twin
from dta_runtime.worker import create_app as create_worker
from webhook_testkit import own_port

T0 = datetime(2026, 1, 5, tzinfo=UTC)
STEP = timedelta(minutes=15)
BUILT = 24
BASE = {"T1": 5.0, "T2": 8.0}
TOLERANCE = 0.5
QUESTION = "level_ahead"
SPEC = "level"
HOLD = "twin_model:level/hold"
JOB_S = 240.0
WEBHOOK_ENV = "DTA_TEST_FIDELITY_WEBHOOK"
CHECK_PATH = "/fidelity/checks"

Drift = Callable[[int], float]

CODE_MODULE = "drift_plant"
CODE = '''
import pandas as pd


class Hold:
    """The last value of each series it was fitted to, for every step ahead."""

    interventional = False

    def __init__(self, step):
        self.step = step

    def fit(self, frame, params, *, horizon):
        last = frame.sort_values("ds").groupby("unique_id")["y"].last()
        return {str(uid): float(value) for uid, value in last.items()}

    def predict(self, state, history, horizon, *, levels=(), drivers=None, interventions=()):
        rows = []
        for uid, series in history.groupby("unique_id", sort=True):
            value = state.get(str(uid), float(series["y"].iloc[-1]))
            moment = series["ds"].iloc[-1]
            for _ in range(horizon):
                moment = moment + self.step
                row = {"unique_id": uid, "ds": moment, "mean": value}
                for level in levels:
                    row[f"lo_{level:g}"] = value - 0.25
                    row[f"hi_{level:g}"] = value + 0.25
                rows.append(row)
        return pd.DataFrame(rows)

    def validity(self, frame):
        return {"y": (float(frame["y"].min()), float(frame["y"].max()))}


def hold(*, step, twin):
    return Hold(step)
'''


def at(steps: float) -> datetime:
    return T0 + steps * STEP


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def check_key(step: int) -> str:
    """The key the scheduler asks for the check of a boundary with."""
    return f"fidelity:{at(step).isoformat()}"


def code() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as written:
        written.writestr(zipfile.ZipInfo(f"{CODE_MODULE}.py"), CODE)
    return buffer.getvalue()


def none(step: int) -> float:
    return 0.0


def shift(amount: float, since: int = BUILT + 1) -> Drift:
    """Both levels `amount` higher from step `since` on."""
    return lambda step: amount if step >= since else 0.0


def alternating(amount: float, since: int = BUILT + 1) -> Drift:
    """Both levels `amount` above and below where they were, step by step, from `since`."""
    return lambda step: (amount if step % 2 else -amount) if step >= since else 0.0


def manifest(
    ontology_text: str,
    *,
    notification: Mapping[str, Any] | None = None,
    correction: Mapping[str, Any] | None = None,
    window_steps: int = 6,
) -> Any:
    question = {
        "text": "How full will each tank be in half an hour?",
        "kind": "predictive",
        "model": SPEC,
        "horizon": "PT30M",
        "tolerance": {"metric": "mae", "value": TOLERANCE, "unit": "m"},
    }
    contract: dict[str, Any] = {
        "tier": "live",
        "questions": {QUESTION: question},
        "boundary": {"state": ["Tank.level"], "observations": ["Tank.level"]},
        "notification": dict(notification or {"kind": "log"}),
    }
    sections: dict[str, Any] = {
        "contract": contract,
        "functions": {},
        "models": {
            SPEC: {
                "question": QUESTION,
                "target": "Tank.level",
                "candidates": {"hold": {"family": f"{CODE_MODULE}:hold"}},
                "features": [],
                "backtest": {"initial_window": "PT2H", "step": "PT15M", "min_folds": 3},
                "acceptance": {"metric": "mae", "threshold": TOLERANCE},
            }
        },
        "sync": {"interval": "PT15M"},
        "fidelity": {
            "interval": "PT15M",
            "window": int((window_steps * STEP).total_seconds()),
            "correction": {
                "recalibrate_attempts": 1,
                "reselect": True,
                "agent_remodel": True,
                "retry_budget": 1,
                "remodel_timeout": "PT1H",
                **dict(correction or {}),
            },
        },
    }
    return manifest_for(ontology_text, **sections)


class Clock(FixedClock):
    """A clock the test moves by hand, and can make unreadable."""

    def __init__(self, moment: datetime) -> None:
        super().__init__(moment)
        self.stopped = False

    def now(self) -> datetime:
        if self.stopped:
            raise ClockUnavailable("the test stopped the clock")
        return super().now()


def story(steps: Mapping[int, tuple[dict[str, Any], list[dict[str, Any]]]]) -> str:
    """What each step's check and corrections came to, one line each: for messages."""
    lines = []
    for step, (job, corrections) in sorted(steps.items()):
        result = job.get("result") or {}
        done = [
            (
                (found.get("result") or {}).get("rung"),
                (found.get("result") or {}).get("accepted"),
                (found.get("result") or {}).get("reason_code"),
            )
            for found in corrections
        ]
        lines.append(
            f"{step}: {result.get('state')} events={result.get('events')} "
            f"opened={len(result.get('opened') or [])} changed={len(result.get('changed') or [])} "
            f"resolved={len(result.get('resolved') or [])} corrections={done}"
        )
    return "\n".join(lines)


def held(gate: threading.Event) -> Work:
    """A job of the test's that holds its lane until the gate opens."""
    from dta_runtime.api.jobs import Outcome

    def work() -> Outcome:
        assert gate.wait(120), "the test never let the job go"
        return Outcome(result={"held": True})

    return work


@dataclass
class Checked(Calls):
    """The twin behind its API and worker, and the steps the test drives."""

    twin: Twin = None  # type: ignore[assignment]
    app: Any = None
    clock: Clock = None  # type: ignore[assignment]
    settings: RuntimeSettings = None  # type: ignore[assignment]
    drift: Drift = none
    url: str = ""
    api_port: int = 0
    worker_url: str = ""
    correction_budget_s: float = 900.0
    written: dict[int, VersionRef] = field(default_factory=dict)
    stacks: dict[str, contextlib.ExitStack] = field(default_factory=dict)

    @property
    def services(self) -> Any:
        return self.app.state.services

    @property
    def head(self) -> VersionRef:
        return self.twin.store.head()

    def level(self, key: str, step: int) -> float:
        return BASE[key] + (self.drift(step) if step > BUILT else 0.0)

    def write(self, step: int) -> VersionRef:
        """The step's observations, as a sync writes them."""
        rows = [tank(key, self.level(key, step)) for key in sorted(BASE)]
        self.written[step] = observe(self.twin, at(step), {"Tank": rows})
        return self.written[step]

    def ask(self, step: int, key: str | None = None) -> Any:
        """Ask for the check of a step's boundary, with the clock at it."""
        self.clock.set(at(step))
        return self.post(CHECK_PATH, headers={"Idempotency-Key": key or check_key(step)})

    def job(self, job_id: str, timeout_s: float = JOB_S) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        pause = threading.Event()
        while True:
            found = self.ok(f"/lifecycle/jobs/{job_id}")["data"]
            if found["state"] in ("succeeded", "failed"):
                return found
            assert time.monotonic() < deadline, f"job {job_id} still {found['state']}"
            pause.wait(0.05)

    def settle(self, checked: dict[str, Any]) -> list[dict[str, Any]]:
        """The corrections a finished check queued, once they have ended."""
        return [self.job(found["job"]) for found in checked["result"]["corrections"]]

    def step(self, step: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        self.write(step)
        checked = self.wait(self.ask(step), JOB_S)
        assert checked["state"] == "succeeded", checked
        return checked, self.settle(checked)

    def run(self, first: int, last: int) -> dict[int, tuple[dict[str, Any], list[dict[str, Any]]]]:
        return {step: self.step(step) for step in range(first, last + 1)}

    def fidelity(self) -> dict[str, Any]:
        return self.ok("/fidelity")

    def incidents(self, **params: Any) -> list[dict[str, Any]]:
        return self.ok("/fidelity/incidents", **params)["data"]

    def store_rows(self, type_name: str) -> dict[str, dict[str, Any]]:
        rows = self.twin.store.table(type_name, ontology=self.twin.ontology).to_pylist()
        return {row["uid"]: row for row in rows}

    def models(self) -> dict[str, dict[str, Any]]:
        return self.store_rows("TwinModel")

    def gate(self) -> threading.Event:
        """Hold the compute lane until the returned gate is set."""
        gate = threading.Event()
        self.services.jobs.submit("probe", {}, held(gate), lane="compute")
        return gate

    # -- the API's process ------------------------------------------------------------

    def start_api(self) -> None:
        stack = contextlib.ExitStack()
        try:
            self.app = create_app(
                self.settings,
                self.twin,
                clock=self.clock,
                notify_backoff_s=(0.05, 0.1),
                correction_budget_s=self.correction_budget_s,
            )
            self.url = stack.enter_context(serve_on(self.app, self.api_port))
            self.client = stack.enter_context(
                httpx.Client(base_url=self.url, timeout=httpx.Timeout(JOB_S))
            )
        except BaseException:
            stack.close()
            raise
        self.stacks["api"] = stack

    def stop_api(self) -> None:
        """Stop the API as its process would end: what is queued is lost."""
        self.stacks.pop("api").close()

    def stop_worker(self) -> None:
        self.stacks.pop("worker").close()


@contextlib.contextmanager
def serve_checked(
    store: StoreSettings,
    ontology_text: str,
    root: Path,
    *,
    drift: Drift = none,
    notification: Mapping[str, Any] | None = None,
    correction: Mapping[str, Any] | None = None,
    correction_budget_s: float = 900.0,
    evaluate: bool = True,
) -> Iterator[Checked]:
    """Install the twin, write its data up to BUILT, serve its API and a worker, keep its
    code and evaluate its models at BUILT."""
    text = ontology_text
    bundle = Bundle(
        manifest=manifest(text, notification=notification, correction=correction),
        ontology_text=text,
    )
    twin = install_twin(store, bundle, t=T0).twin
    api_port, worker_port = own_port(), 0
    worker_port = own_port({api_port})
    world = Checked(
        client=None,
        twin=twin,
        clock=Clock(at(BUILT)),
        settings=runtime_settings(store, root, f"http://127.0.0.1:{worker_port}"),
        drift=drift,
        api_port=api_port,
        worker_url=f"http://127.0.0.1:{worker_port}",
        correction_budget_s=correction_budget_s,
    )
    try:
        for step in range(1, BUILT + 1):
            world.write(step)
        world.start_api()
        worker_stack = contextlib.ExitStack()
        world.stacks["worker"] = worker_stack
        worker = create_worker(
            WorkerSettings(
                api_url=world.url, api_token=world.settings.api_token, cpus=1.0, memory_mb=2048
            ),
            work_dir=root / "worker",
        )
        worker_stack.enter_context(serve_on(worker, worker_port))
        if evaluate:
            world.register(world.upload(code()))
            evaluated = world.done(
                world.post("/models/evaluate", json={}, headers={"Idempotency-Key": "build"})
            )
            (found,) = evaluated["specs"]
            assert found["selected"] == HOLD, found
        yield world
    finally:
        for name in ("api", "worker"):
            stack = world.stacks.pop(name, None)
            if stack is not None:
                stack.close()
        twin.close()
