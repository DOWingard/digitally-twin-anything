"""A live twin that checks itself and acts: the kit's plant with a minute's readings, a
meter of what flows into each site, a forecast of each, a control on the inflow carried
out by an HTTP call, a loop and an interval function of the twin's own code.

The plant's control interface is a stand-in: a small HTTP server in a container of the
runtime image, on a network of its own that the twin joins under the name `plant`, and
published on 127.0.0.1 so that the test can read what it was sent. It answers every
POST with 200 and keeps the request; `GET /_received` lists them. The twin's webhook
notifications are sent to it as well.

The data is made from the wall clock when the kit is made (T0, a whole minute): one
reading per sensor and minute, and one meter reading per site and minute, for the
`HISTORY` minutes up to T0. The twin's load reads every row in the files, so rows after
T0 are written only once the first build is done (`extend`); from then on each sync run
reads the minute that has just ended, as a plant that keeps delivering would.

    level(sensor, m) = BASE[sensor] + 0.3·(m mod 2) + 0.001·m       every minute
    inflow(site, m)  = FLOW[site] + 0.1·(m mod 2)                   every minute

so every signal changes at every reading. `freeze` makes s1 keep one value from two
minutes on (stuck) and stops the meter's rows (its question goes stale); `restore`
lets s1 change again and gives the meter its rows back.

Its ids start with `m6tk-`: its stack is `dta-m6tk-…`, the stand-in's container and
network `dta-m6tk-…-plant`. All are removed by name when the test is done.
"""

from __future__ import annotations

import copy
import os
import secrets
import shutil
import socket
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml
from m3tk_kit import DATA_ENV, RUNTIME_IMAGE, SENSORS, SITES, KitTwin, Runner, manifest

PROJECT_PREFIX = "dta-m6tk-"
STANDIN_PORTS = range(19000, 19020)
STANDIN_START_S = 30.0
HISTORY = 90
AHEAD = 150
BASE = {"n1": 1.0, "n2": 0.5, "s1": 3.0}
FLOW = {"north": 2.0, "south": 1.0}
FROZEN = 9.99
STAMP = "%Y-%m-%dT%H:%M:%SZ"
CODE_DIR = "code"
HOOK_PATH = "/hooks"
PLANT_URL = "http://plant:8080"

ONTOLOGY = """\
id: https://example.org/dta/m6tk-plant
name: m6tk_plant
description: Sites, the level sensors installed at them and what flows into each site.
prefixes:
  linkml: https://w3id.org/linkml/
  plant: https://example.org/dta/m6tk-plant/
default_prefix: plant
default_range: string
imports:
  - linkml:types

classes:
  Site:
    description: A place where sensors are installed.
    attributes:
      uid:
        identifier: true
        required: true
      name:
        required: true
      inflow:
        range: float
        description: Water let into the site per minute, set through the plant's controls.
        unit:
          ucum_code: m
        annotations:
          dta_role: state

  Sensor:
    description: A level sensor at a site.
    attributes:
      uid:
        identifier: true
        required: true
      site:
        range: Site
        required: true
        description: Site the sensor is installed at.
      kind:
        description: What the sensor measures.
      level:
        range: float
        minimum_value: 0
        description: Measured level above the floor.
        unit:
          ucum_code: m
        annotations:
          dta_role: state
"""

PLANT_CODE = '''\
"""The plant's own code: the policy of its inflow loop, and a function run every hour."""


def policy(twin, site="site:north", value=3.0):
    return {
        "actions": [
            {
                "action": "set_inflow",
                "object": site,
                "params": {"value": value},
                "reason": "the policy of the test plant",
            }
        ],
        "notes": ["one proposal for the north works"],
    }


def quarter(twin):
    return {"sites": len(twin.get("/objects/Site")["data"])}
'''

STANDIN = """\
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RECEIVED = []
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def answer(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/_received":
            with LOCK:
                self.answer(200, list(RECEIVED))
        else:
            self.answer(404, {"error": "no such path"})

    def keep(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode("utf-8", "replace")
        headers = {name.lower(): value for name, value in self.headers.items()}
        with LOCK:
            RECEIVED.append({"method": self.command, "path": self.path, "headers": headers,
                             "body": body})
        self.answer(200, {"accepted": True})

    do_POST = keep
    do_PUT = keep
    do_PATCH = keep

    def log_message(self, format, *args):
        sys.stderr.write("stand-in: " + (format % args) + "\\n")


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
"""


# -- data -------------------------------------------------------------------------------


def minute_floor(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


def level(sensor: str, minute: int) -> float:
    return round(BASE[sensor] + (0.3 if minute % 2 else 0.0) + 0.001 * minute, 4)


def inflow(site: str, minute: int) -> float:
    return round(FLOW[site] + (0.1 if minute % 2 else 0.0), 4)


def readings(t0: datetime, until: int, frozen: tuple[int, int] | None = None) -> str:
    """One reading per sensor and minute from `HISTORY` minutes before `t0` to `until`
    minutes after it; s1 keeps `FROZEN` for the minutes in `frozen` [start, end)."""
    lines = ["sensor_id,timestamp,level"]
    for minute in range(-HISTORY, until + 1):
        stamp = (t0 + timedelta(minutes=minute)).strftime(STAMP)
        for sensor in BASE:
            value = level(sensor, minute)
            if sensor == "s1" and frozen is not None and frozen[0] <= minute < frozen[1]:
                value = FROZEN
            lines.append(f"{sensor},{stamp},{value}")
    return "\n".join(lines) + "\n"


def flows(t0: datetime, until: int) -> str:
    lines = ["site_id,timestamp,inflow"]
    for minute in range(-HISTORY, until + 1):
        stamp = (t0 + timedelta(minutes=minute)).strftime(STAMP)
        lines.extend(f"{site},{stamp},{inflow(site, minute)}" for site in FLOW)
    return "\n".join(lines) + "\n"


def _replace(path: Path, text: str) -> None:
    """The file's new content at once, so a sync never reads half of it."""
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)


# -- the manifest ----------------------------------------------------------------------


def m6_manifest(twin_id: str, network: str) -> dict[str, Any]:
    data = copy.deepcopy(manifest(twin_id))
    contract = data["contract"]
    contract["purpose"] = "Show and forecast the levels of the plant and steer what flows in"
    contract["tier"] = "live"
    contract["notification"] = {"kind": "log"}
    level_question = contract["questions"]["q_level"]
    level_question["staleness"] = "PT1H"
    level_question["tolerance"]["value"] = 1.0
    contract["questions"]["q_ahead"] = {
        "text": "What will the level of each sensor be in five minutes?",
        "kind": "predictive",
        "priority": 2,
        "tolerance": {"metric": "mae", "value": 50.0, "unit": "m"},
        "staleness": "P1D",
        "targets": ["Sensor.level"],
        "function": "f_forecast",
        "model": "level",
        "horizon": "PT5M",
    }
    # The fidelity check judges the freshness of the questions it forecasts.
    contract["questions"]["q_flow"] = {
        "text": "How much will flow into each site in five minutes?",
        "kind": "predictive",
        "priority": 3,
        "tolerance": {"metric": "mae", "value": 5.0, "unit": "m"},
        "staleness": "PT30M",
        "targets": ["Site.inflow"],
        "function": "f_forecast_flow",
        "model": "flow",
        "horizon": "PT5M",
    }
    contract["boundary"] = {
        "state": ["Sensor.level", "Site.inflow"],
        "controls": ["Site.inflow"],
        "observations": ["Sensor.level", "Site.inflow"],
    }
    contract["controls"] = {
        "c_inflow": {
            "description": "What flows into a site",
            "target": "Site.inflow",
            "bounds": {"minimum": 0, "maximum": 10},
            "autonomy": "supervised",
            "action": "set_inflow",
        }
    }
    contract["objective"] = {
        "description": "Levels that follow the plan",
        "direction": "minimize",
        "kpi": "f_latest",
    }
    contract["addons"] = {"executors": True}
    data["sources"]["meter"] = {
        "description": "CSV file of the plant's inflow meters",
        "connector": "file",
        "location": f"file://${{{DATA_ENV}}}",
        "env": [DATA_ENV],
        "options": {"datasets": {"flows": {"glob": "flows.csv", "format": "csv"}}},
        "mappings": {
            "inflows": {
                "dataset": "flows",
                "object_type": "Site",
                "identity": ["site_id"],
                "valid_time": "timestamp",
                "properties": {"inflow": {"column": "inflow", "unit": "m"}},
            }
        },
    }
    data["sync"] = {"backfill_interval": "PT1M"}
    # The stuck test looks back over the window, and counts its intervals before the
    # history began as intervals without a change: the window lies inside the history.
    data["fidelity"] = {"interval": "PT1M", "window": "PT1H"}
    data["runtime"] = {"image": RUNTIME_IMAGE, "code": CODE_DIR, "networks": [network]}
    data["functions"]["f_forecast"] = {
        "description": "Forecast of the question's target by its selected model",
        "kind": "builtin",
        "entry": "forecast",
        "params": {"question": "q_ahead"},
        "complexity": "O(|V|)",
        "latency_budget": "PT60S",
    }
    data["functions"]["f_forecast_flow"] = {
        "description": "Forecast of the inflow by its selected model",
        "kind": "builtin",
        "entry": "forecast",
        "params": {"question": "q_flow"},
        "complexity": "O(|V|)",
        "latency_budget": "PT60S",
    }
    data["functions"]["f_policy"] = {
        "description": "What the inflow loop should do now",
        "kind": "registered",
        "entry": "plant:policy",
        "complexity": "O(1)",
        "latency_budget": "PT30S",
    }
    data["functions"]["f_quarter"] = {
        "description": "How many sites there are, every hour",
        "kind": "registered",
        "entry": "plant:quarter",
        "complexity": "O(|V|)",
        "latency_budget": "PT30S",
        "trigger": "interval",
        "interval": "PT1H",
    }
    data["models"] = {
        "level": {
            "question": "q_ahead",
            "target": "Sensor.level",
            "candidates": {"persist": {"family": "persistence"}},
            "features": ["Sensor.level"],
            "backtest": {"initial_window": "PT30M", "step": "PT10M", "min_folds": 3},
            "acceptance": {"metric": "mae", "threshold": 50.0},
        },
        "flow": {
            "question": "q_flow",
            "target": "Site.inflow",
            "candidates": {"persist": {"family": "persistence"}},
            "features": ["Site.inflow"],
            "backtest": {"initial_window": "PT30M", "step": "PT10M", "min_folds": 3},
            "acceptance": {"metric": "mae", "threshold": 50.0},
        },
    }
    data["actions"] = {
        "set_inflow": {
            "description": "Set what flows into a site through the plant's control interface",
            "control": "c_inflow",
            "object_type": "Site",
            "binding": "executor",
            "executor": {
                "kind": "http",
                "url": f"{PLANT_URL}/api/v1/controls/{{id}}",
                "method": "POST",
                "env": [],
                "body": {"setpoint": "{value}"},
            },
            "parameters": {"value": {"type": "number", "unit": "m"}},
            "settle": "PT5S",
            "tolerance": 0.5,
        }
    }
    data["control_loops"] = {
        "l_inflow": {
            "description": "Keeps the north works' inflow on plan",
            "controls": ["c_inflow"],
            "policy": "f_policy",
            "interval": "PT1H",
        }
    }
    return data


# -- docker, by name only --------------------------------------------------------------


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def _ids(*args: str) -> list[str]:
    return [line for line in _docker(*args).stdout.split() if line]


def _label(project: str) -> str:
    return f"label=com.docker.compose.project={project}"


def remove_project(project: str) -> None:
    """Remove every container, volume and network of one of this kit's stacks."""
    assert project.startswith(PROJECT_PREFIX), project
    if shutil.which("docker") is None:
        return
    containers = _ids("ps", "-aq", "--filter", _label(project))
    if containers:
        _docker("rm", "-f", *containers)
    volumes = _ids("volume", "ls", "-q", "--filter", _label(project))
    if volumes:
        _docker("volume", "rm", *volumes)
    networks = _ids("network", "ls", "-q", "--filter", _label(project))
    if networks:
        _docker("network", "rm", *networks)


def leftovers(project: str) -> dict[str, list[str]]:
    return {
        "containers": _ids("ps", "-aq", "--filter", _label(project)),
        "volumes": _ids("volume", "ls", "-q", "--filter", _label(project)),
        "networks": _ids("network", "ls", "-q", "--filter", _label(project)),
    }


def service_container(project: str, service: str) -> str | None:
    """The id of the running container of one service of a stack."""
    found = _ids(
        "ps",
        "-q",
        "--filter",
        _label(project),
        "--filter",
        f"label=com.docker.compose.service={service}",
    )
    return found[0] if found else None


def free_standin_port() -> int:
    for port in secrets.SystemRandom().sample(list(STANDIN_PORTS), len(STANDIN_PORTS)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("no free port in 19000-19019")


class StandIn:
    """The plant's control interface: a container on a network of its own."""

    def __init__(self, name: str) -> None:
        assert name.startswith(PROJECT_PREFIX), name
        self.name = name
        self.network = name
        self.port = free_standin_port()
        self.url = f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        done = _docker("network", "create", self.network)
        assert done.returncode == 0, done.stderr
        done = _docker(
            "run",
            "--detach",
            "--name",
            self.name,
            "--network",
            self.network,
            "--network-alias",
            "plant",
            "--publish",
            f"127.0.0.1:{self.port}:8080",
            "--entrypoint",
            "python",
            RUNTIME_IMAGE,
            "-c",
            STANDIN,
        )
        assert done.returncode == 0, done.stderr
        deadline = time.monotonic() + STANDIN_START_S
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.url}/_received", timeout=2.0).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise AssertionError(f"the stand-in did not answer within {STANDIN_START_S} s")

    def received(self, path_prefix: str = "") -> list[dict[str, Any]]:
        found = httpx.get(f"{self.url}/_received", timeout=10.0).json()
        return [entry for entry in found if entry["path"].startswith(path_prefix)]

    def _container_exists(self) -> bool:
        return _docker("container", "inspect", self.name).returncode == 0

    def _network_exists(self) -> bool:
        return _docker("network", "inspect", self.network).returncode == 0

    def stop(self) -> None:
        """Its container, then its network, each by name; the twin has left it by then."""
        if self._container_exists():
            _docker("rm", "-f", self.name)
        if self._network_exists():
            _docker("network", "rm", self.network)

    def gone(self) -> bool:
        return not self._container_exists() and not self._network_exists()


# -- the twin ---------------------------------------------------------------------------


class M6Twin(KitTwin):
    """The kit's plant as a live twin that acts; its stack and stand-in go afterwards."""

    def __init__(self, dta: Runner, base: Path) -> None:
        super().__init__(dta, base, live=True)
        self.id = f"m6tk-{secrets.token_hex(4)}"
        self.project = f"dta-{self.id}"
        self.standin = StandIn(f"{self.project}-plant")
        self.t0 = minute_floor(datetime.now(UTC))
        self.until = 0
        self.flows_until = 0
        self.frozen: tuple[int, int] | None = None
        (self.data / "sites.csv").write_text(SITES, encoding="utf-8")
        (self.data / "sensors.csv").write_text(SENSORS, encoding="utf-8")
        self._write_data()

    def _write_data(self) -> None:
        _replace(self.data / "readings.csv", readings(self.t0, self.until, self.frozen))
        _replace(self.data / "flows.csv", flows(self.t0, self.flows_until))

    def minute(self) -> int:
        """Whole minutes since T0 on the wall."""
        return int((datetime.now(UTC) - self.t0) // timedelta(minutes=1))

    def at(self, minute: int) -> datetime:
        return self.t0 + timedelta(minutes=minute)

    def extend(self) -> None:
        """The plant keeps delivering: rows for the next `AHEAD` minutes, read as they end."""
        self.until = self.flows_until = AHEAD
        self._write_data()

    def freeze(self) -> int:
        """s1 keeps one value from two minutes on, and the meter stops; the first frozen
        minute."""
        now = self.minute()
        self.frozen = (now + 2, AHEAD + 1)
        self.flows_until = now
        self._write_data()
        return now + 2

    def restore(self) -> int:
        """s1 changes again from two minutes on, and the meter's rows are back."""
        assert self.frozen is not None
        now = self.minute()
        self.frozen = (self.frozen[0], now + 2)
        self.flows_until = AHEAD
        self._write_data()
        return now + 2

    def write(self) -> None:
        self.ok("init", "--id", self.id)
        (self.root / "ontology").mkdir(exist_ok=True)
        (self.root / "ontology" / "plant.yaml").write_text(ONTOLOGY, encoding="utf-8")
        (self.root / CODE_DIR).mkdir(exist_ok=True)
        (self.root / CODE_DIR / "plant.py").write_text(PLANT_CODE, encoding="utf-8")
        self.root.joinpath("manifest.yaml").write_text(
            yaml.safe_dump(m6_manifest(self.id, self.standin.network), sort_keys=False),
            encoding="utf-8",
        )

    def prepare(self) -> None:
        self.standin.start()
        super().prepare()

    def set_env(self, name: str, value: str) -> None:
        """Set one name in `.env`, as the user would; the file keeps its mode."""
        lines = [
            line
            for line in self.env_file.read_text(encoding="utf-8").splitlines()
            if line.partition("=")[0] != name
        ]
        self.env_file.write_text("\n".join([*lines, f"{name}={value}"]) + "\n", encoding="utf-8")

    def change(self, change: Any, *, sign: bool) -> None:
        """Change the manifest and validate it; sign it again when the contract changed."""
        data = self.dta.read_manifest()
        change(data)
        self.dta.write_manifest(data)
        self.ok("manifest", "validate")
        if sign:
            self.ok("manifest", "confirm", "--all-user")
            self.ok("manifest", "validate")
            self.ok("manifest", "sign", "--non-interactive", "--approved-by", "Test Operator")

    def api_container(self) -> str | None:
        return service_container(self.project, "api")

    def text(self, *args: str) -> tuple[int, str]:
        """A command as a person runs it, without `--json`: its exit code and its output."""
        result = self.dta.run(*args)  # type: ignore[attr-defined]
        self.outputs.append(result.output)
        return result.exit_code, result.output

    def teardown(self) -> None:
        try:
            if (self.root / "compose.yaml").is_file():
                self.dta.json("down", "--destroy-data")
            remove_project(self.project)
        finally:
            self.standin.stop()
        left = leftovers(self.project)
        assert not any(left.values()), left
        assert self.standin.gone(), self.standin.name


__all__ = [
    "AHEAD",
    "FROZEN",
    "HOOK_PATH",
    "PLANT_URL",
    "PROJECT_PREFIX",
    "M6Twin",
    "leftovers",
    "m6_manifest",
    "service_container",
]
