"""A small water twin with controls for the action tests, a plant that answers its
calls over HTTP, and what the tests read back.

The network, on the water ontology with one property added (`Pump.running`):

    PMP-10: J1 -> J2, running false        VLV-1: J1 -> J2, setting 5.0
    VLV-2:  J1 -> J2, no setting           (the junctions J1 and J2)

The values are observed at step 1 (15 minutes each after T0). The manifest delegates:

    pump_running   Pump.running    autonomous   set_pump      (executor)
    valve_setting  Valve.setting   supervised   set_valve     (executor; 1 to 200, max_step 50)
    pump_speed     Pump.speed      advisory     advise_speed  (executor; allowed 0.8, 0.9, 1.0)

with two more action types: `suggest_pump` (bound advisory, on pump_running) and
`note_valve` (no control, bound advisory, a whole-number `priority` from 1 to 3). The
loop `pumping` drives pump_running and valve_setting with the policy `pump_policy`.
The executors call `${PLANT_URL}` with `${PLANT_TOKEN}`, the secret.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import yaml
from dta_core.log import JsonFormatter
from dta_core.manifest import Manifest, parse_manifest
from dta_core.ontology import parse_ontology, type_signature
from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance, StoreSettings, VersionRef
from dta_runtime.actions import Execution, HttpExecutor, known, rows
from dta_runtime.twin import Bundle, Twin, install_twin
from werkzeug.serving import WSGIRequestHandler, make_server

T0 = datetime(2026, 1, 5, tzinfo=UTC)
STEP = timedelta(minutes=15)
PLANT_PORT = 18940
REFUSING_PORT = 18941
LATE_PORT = 18942
TOKEN = "Tk-9f3a71c2e5b8d406"
PUMP, VALVE, BARE_VALVE = "link:PMP-10", "link:VLV-1", "link:VLV-2"
J1, J2 = "node:J1", "node:J2"
LOOP = "twin_control_loop:pumping"
POLICY = "pump_policy"
POLICY_BUDGET_MS = 2500

Row = dict[str, Any]

_PUMP_HEAD = "  Pump:\n    is_a: Link\n    attributes:\n"
_RUNNING = (
    "      running:\n        range: boolean\n        annotations:\n          dta_role: state\n"
)


def at(steps: float) -> datetime:
    return T0 + steps * STEP


def ontology_text(water_text: str) -> str:
    """The water ontology with a pump's running state."""
    assert _PUMP_HEAD in water_text
    return water_text.replace(_PUMP_HEAD, _PUMP_HEAD + _RUNNING)


def executor_spec(**changes: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "kind": "http",
        "url": "${PLANT_URL}/api/v1/controls/{id}?token=${PLANT_TOKEN}",
        "method": "POST",
        "env": ["PLANT_URL", "PLANT_TOKEN"],
        "headers": {"X-API-Key": "${PLANT_TOKEN}", "X-Object": "{uid}"},
        "body": {
            "status": {"from": "value", "map": {"true": "on", "false": "off"}},
            "asset": "{id}",
            "note": "set {value} on {uid}",
        },
    }
    spec.update(changes)
    return spec


def manifest_document(ontology: str) -> dict[str, Any]:
    return {
        "twin": {"id": "water", "name": "Water network", "revision": 1},
        "contract": {
            "tier": "live",
            "addons": {"executors": True},
            "boundary": {
                "state": ["Pump.running", "Valve.setting"],
                "controls": ["Pump.running", "Valve.setting", "Pump.speed"],
                "observations": ["Pump.running", "Valve.setting"],
            },
            "controls": {
                "pump_running": {
                    "description": "Whether the lake pump runs",
                    "target": "Pump.running",
                    "bounds": {"allowed": [True, False]},
                    "autonomy": "autonomous",
                    "action": "set_pump",
                    "on_failure": {"mode": "hold"},
                },
                "valve_setting": {
                    "description": "Loss coefficient of a valve",
                    "target": "Valve.setting",
                    "bounds": {"minimum": 1.0, "maximum": 200.0, "max_step": 50.0},
                    "autonomy": "supervised",
                    "action": "set_valve",
                },
                "pump_speed": {
                    "description": "Relative speed of a pump",
                    "target": "Pump.speed",
                    "bounds": {"allowed": [0.8, 0.9, 1.0]},
                    "autonomy": "advisory",
                    "action": "advise_speed",
                },
            },
        },
        "ontology": {
            "path": "ontology/water_network.yaml",
            "type_signature": type_signature(parse_ontology(ontology).ontology),
        },
        "sync": {"interval": "PT15M"},
        "functions": {
            POLICY: {
                "kind": "registered",
                "entry": "plant:policy",
                "complexity": "O(1)",
                "latency_budget": "PT2.5S",
            }
        },
        "actions": {
            "set_pump": {
                "description": "Start or stop a pump",
                "control": "pump_running",
                "binding": "executor",
                "executor": executor_spec(),
                "parameters": {"value": {"type": "boolean"}},
                "expected_effect": "the pump runs as asked",
            },
            "set_valve": {
                "description": "Set a valve's loss coefficient",
                "control": "valve_setting",
                "binding": "executor",
                "executor": executor_spec(
                    method="PUT",
                    url="${PLANT_URL}/api/v1/controls/{id}",
                    headers={"X-API-Key": "${PLANT_TOKEN}"},
                    body={"setting": "{value}"},
                ),
                "parameters": {"value": {"type": "number", "unit": "1"}},
                "settle": "PT45M",
                "tolerance": 0.5,
            },
            "advise_speed": {
                "control": "pump_speed",
                "binding": "executor",
                "executor": executor_spec(body={"speed": "{value}"}),
                "parameters": {"value": {"type": "number"}},
            },
            "suggest_pump": {
                "control": "pump_running",
                "binding": "advisory",
                "parameters": {"value": {"type": "boolean"}},
            },
            "note_valve": {
                "object_type": "Valve",
                "binding": "advisory",
                "parameters": {
                    "text": {"type": "string"},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 3},
                },
            },
        },
        "control_loops": {
            "pumping": {
                "description": "Keeps the pump and the valve where the policy wants them",
                "controls": ["pump_running", "valve_setting"],
                "policy": POLICY,
                "interval": "PT15M",
            }
        },
    }


def manifest_of(document: Mapping[str, Any]) -> Manifest:
    return parse_manifest(yaml.safe_load(yaml.safe_dump(dict(document))), origin="test manifest")


def environ(url: str = f"http://127.0.0.1:{PLANT_PORT}") -> dict[str, str]:
    return {"PLANT_URL": url, "PLANT_TOKEN": TOKEN, "UNRELATED": "nothing"}


def executor(manifest: Manifest, url: str | None = None, **options: Any) -> HttpExecutor:
    options.setdefault("timeout_s", 2.0)
    options.setdefault("backoff_s", 0.05)
    found = HttpExecutor.from_manifest(manifest, environ(url) if url else environ(), **options)
    assert found is not None
    return found


# -- the twin ----------------------------------------------------------------------------


def observe(twin: Twin, t: datetime, upserts: Mapping[str, list[Row]]) -> VersionRef:
    """One version of observed facts, as a sync writes them."""
    changes = ChangeSet(
        upserts=dict(upserts),
        provenance=Provenance(source="scada", kind=FactKind.observed, t_valid=t),
    )
    return twin.store.commit(changes, CommitMeta(cause="sync", actor="scada", t=t))


def infer(twin: Twin, t: datetime, upserts: Mapping[str, list[Row]]) -> VersionRef:
    """One version of values the twin estimated, not observed."""
    changes = ChangeSet(
        upserts=dict(upserts),
        provenance=Provenance(source="twin_model:estimate", kind=FactKind.inferred, t_valid=t),
    )
    return twin.store.commit(changes, CommitMeta(cause="derived", actor="fidelity", t=t))


def register_rows(twin: Twin, upserts: Mapping[str, list[Row]]) -> VersionRef:
    """Records of the twin about itself, at the time of the live head."""
    head = twin.store.head()
    changes = ChangeSet(
        upserts={name: found for name, found in upserts.items() if found},
        provenance=Provenance(source="registry", kind=FactKind.inferred, t_valid=head.t),
    )
    return twin.store.commit(changes, CommitMeta(cause="registration", actor="test", t=head.t))


def policy_function_row() -> Row:
    return {
        "uid": f"twin_function:{POLICY}",
        "kind": "registered",
        "entry": "plant:policy",
        "trigger": "on_request",
        "latency_budget_ms": POLICY_BUDGET_MS,
        "origin": "manifest",
        "status": "active",
    }


def register(twin: Twin, manifest: Manifest) -> Any:
    """Bring the twin's action types and loops in line with a manifest, with the policy's
    function in the same version, as the API registers them."""
    found = rows(manifest, known(twin))
    upserts = {name: list(found_rows) for name, found_rows in found.upserts.items()}
    if twin.store.get(f"twin_function:{POLICY}") is None:
        upserts["TwinFunction"] = [policy_function_row()]
    if any(upserts.values()):
        register_rows(twin, upserts)
    return found


def link(uid: str, **values: Any) -> Row:
    return {"uid": uid, "start_node": J1, "end_node": J2, "status": "open", **values}


@dataclass
class World:
    twin: Twin
    manifest: Manifest
    document: dict[str, Any]
    ontology: str

    @property
    def store(self):
        return self.twin.store


def build_world(settings: StoreSettings, water_text: str) -> World:
    text = ontology_text(water_text)
    document = manifest_document(text)
    manifest = manifest_of(document)
    installed = install_twin(settings, Bundle(manifest=manifest, ontology_text=text), t=T0)
    twin = installed.twin
    observe(
        twin,
        at(1),
        {
            "Junction": [
                {"uid": J1, "name": "J1", "elevation": 40.0},
                {"uid": J2, "name": "J2", "elevation": 38.0},
            ],
            "Pump": [link(PUMP, running=False, speed=1.0)],
            "Valve": [
                link(VALVE, valve_type="tcv", setting=5.0),
                link(BARE_VALVE, valve_type="tcv"),
            ],
        },
    )
    register(twin, manifest)
    return World(twin=twin, manifest=manifest, document=document, ontology=text)


# -- the plant ---------------------------------------------------------------------------


class _Quiet(WSGIRequestHandler):
    """The plant's own request log would write the addresses it was called at."""

    def log_request(self, *args: Any, **kwargs: Any) -> None:
        pass


@dataclass
class Received:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: Any


class Plant:
    """A local HTTP server standing in for the real system. It keeps what it receives
    and answers as it is told: `ok` (200), `422`, `500` or `hang`."""

    def __init__(self, port: int = PLANT_PORT) -> None:
        self.port = port
        self.received: list[Received] = []
        self.mode = "ok"
        self.effective_at: str | None = None
        self.answer: Any = None
        self.release = threading.Event()
        self._server = make_server(
            "127.0.0.1", port, self._app, threaded=True, request_handler=_Quiet
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def reset(self) -> None:
        self.received.clear()
        self.mode = "ok"
        self.effective_at = None
        self.answer = None

    def _app(self, environ: dict[str, Any], start_response: Callable) -> list[bytes]:
        size = int(environ.get("CONTENT_LENGTH") or 0)
        raw = environ["wsgi.input"].read(size) if size else b""
        headers = {
            name[5:].replace("_", "-").title(): value
            for name, value in environ.items()
            if name.startswith("HTTP_")
        }
        if environ.get("CONTENT_TYPE"):
            headers["Content-Type"] = environ["CONTENT_TYPE"]
        self.received.append(
            Received(
                method=environ["REQUEST_METHOD"],
                path=environ.get("PATH_INFO", ""),
                query=parse_qs(environ.get("QUERY_STRING", "")),
                headers=headers,
                body=json.loads(raw) if raw else None,
            )
        )
        if self.mode == "hang":
            self.release.wait(timeout=30)
        status = {"ok": "200 OK", "422": "422 Unprocessable Entity", "500": "500 Server Error"}
        document = self.answer
        if document is None:
            document = {"accepted": self.mode == "ok"}
            if self.effective_at is not None:
                document["effective_at"] = self.effective_at
        payload = (document if isinstance(document, str) else json.dumps(document)).encode()
        start_response(
            status.get(self.mode, "200 OK"),
            [("Content-Type", "application/json"), ("Content-Length", str(len(payload)))],
        )
        return [payload]

    def stop(self) -> None:
        self.release.set()
        self._server.shutdown()
        self._server.server_close()


class _StartOnRetry(logging.Handler):
    def __init__(self, port: int, made: list[Plant]) -> None:
        super().__init__(logging.WARNING)
        self.port = port
        self.made = made

    def emit(self, record: logging.LogRecord) -> None:
        if not self.made and "trying again" in record.getMessage():
            self.made.append(Plant(self.port))


@contextmanager
def plant_on_first_retry(port: int) -> Iterator[list[Plant]]:
    """A plant that starts listening when the executor first says it will try again: the
    first attempt is refused and the next one, after the wait, is answered."""
    made: list[Plant] = []
    handler = _StartOnRetry(port, made)
    logger = logging.getLogger("dta")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield made
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        for found in made:
            found.stop()


# -- executors of the tests' own ---------------------------------------------------------


class Accepting:
    """Executes everything, as a plant that always answers 200 would; keeps what it was
    asked to send."""

    def __init__(self, effective_at: datetime | None = None) -> None:
        self.calls: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.effective_at = effective_at

    def __call__(self, action: Mapping[str, Any], record: Mapping[str, Any]) -> Execution:
        self.calls.append((dict(action), dict(record)))
        return Execution(
            "executed", http_status=200, duration_ms=1.0, attempts=1, effective_at=self.effective_at
        )


class Died(BaseException):
    """Stands for the process stopping: nothing after it runs."""


class Dying:
    """Makes the call through a real executor, then the process stops before the
    outcome is recorded."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def __call__(self, action: Mapping[str, Any], record: Mapping[str, Any]) -> Execution:
        self.inner(action, record)
        raise Died()


# -- what the tests read back ------------------------------------------------------------


@dataclass
class Captured:
    """Every line logged while it is in place, as it would be written."""

    lines: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(self.lines)

    def messages(self, level: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        found = []
        for line in self.lines:
            entry = json.loads(line)
            if level is None or entry["level"] == level:
                found.append((entry["message"], entry.get("data") or {}))
        return found


class _Keeper(logging.Handler):
    def __init__(self, captured: Captured) -> None:
        super().__init__(logging.DEBUG)
        self.captured = captured
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.captured.lines.append(self.format(record))


@contextmanager
def capture() -> Iterator[Captured]:
    """Keep what the twin and the HTTP library log, at every level."""
    captured = Captured()
    keeper = _Keeper(captured)
    names = ["dta", "httpx", "httpcore"]
    loggers = [logging.getLogger(name) for name in names]
    levels = [found.level for found in loggers]
    propagation = [found.propagate for found in loggers]
    for found in loggers:
        found.addHandler(keeper)
        found.setLevel(logging.DEBUG)
        found.propagate = False
    try:
        yield captured
    finally:
        for found, level, propagate in zip(loggers, levels, propagation, strict=True):
            found.removeHandler(keeper)
            found.setLevel(level)
            found.propagate = propagate


def stored_text(world: World) -> str:
    """Everything the twin keeps about actions, loops and its versions, as text."""
    parts = []
    for type_name in ("TwinAction", "TwinActionRecord", "TwinControlLoop"):
        parts.append(json.dumps(world.store.table(type_name).to_pylist(), default=str))
    for version in world.store.versions():
        parts.append(json.dumps([version.note, dict(version.extra)], default=str))
    return "\n".join(parts)


def record_row(world: World, uid: str, at: Any = "head") -> dict[str, Any]:
    found = world.store.get(uid, at=at)
    assert found is not None, uid
    return {"uid": uid, **found.values}
