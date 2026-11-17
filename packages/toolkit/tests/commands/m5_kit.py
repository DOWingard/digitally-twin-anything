"""A twin that computes: the kit's plant with a day and more of hourly levels, questions
answered by functions and models, its own code and a worker image of its own.

Each sensor's level rises slowly and swings with a four-hour cycle:

    level(sensor, i) = BASE[sensor] + RISE * i + CYCLE[i % 4]      at T0 + i hours

so a seasonal naive forecast is always off by four hours of rise, and a model that
follows the rise (theta) beats it. The manifest declares:

    questions   q_level (latest level, built-in), q_ahead (model `level`),
                q_whatif (model `whatif`, the twin's own interventional family)
    functions   f_latest, f_forecast and f_whatif (built-in), f_report (the twin's own
                code, which needs the one package of the worker's requirements)
    models      level: seasonal_naive and theta, judged against the seasonal baseline;
                whatif: the twin's own drift, which a raised inflow lifts

Its ids start with `m5tk-`, so its stack is `dta-m5tk-…` and its worker image
`dta-worker-m5tk-…`; both are removed by name when the test is done.
"""

from __future__ import annotations

import copy
import json
import secrets
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from m3tk_kit import DATA_ENV, RUNTIME_IMAGE, KitTwin, Runner, manifest

PROJECT_PREFIX = "dta-m5tk-"
IMAGE_PREFIX = "dta-worker-m5tk-"
HOURS = 30
T0 = datetime(2026, 1, 1, tzinfo=UTC)
BASE = {"n1": 2.0, "n2": 1.0, "s1": 3.0}
RISE = 0.05
CYCLE = (0.0, 1.0, 0.5, -0.5)
SEASON = {"season_length": 4}
# A wheel of pure Python with no dependencies, small, and not in the runtime image.
REQUIREMENT = "inflection==0.5.1\n"
REQUIREMENTS_FILE = "worker-requirements.txt"
CODE_DIR = "code"

ONTOLOGY = """\
id: https://example.org/dta/m5tk-plant
name: m5tk_plant
description: Sites, the level sensors installed at them and what flows into each site.
prefixes:
  linkml: https://w3id.org/linkml/
  plant: https://example.org/dta/m5tk-plant/
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
        description: Water let into the site per hour, set by the operator.
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
      estimate:
        range: float
        description: A level worked out by the twin's own functions.
        unit:
          ucum_code: m
"""

PLANT_CODE = '''\
"""The plant's own code: a report of one sensor and a family that lets levels drift."""

import inflection
import pandas as pd


def report(twin, uid):
    found = twin.get(f"/objects/Sensor/{uid}")
    return {"level": found["data"]["values"]["level"], "label": inflection.titleize(uid)}


class Drift:
    """Each step adds `rate`, and whatever a changed inflow lets in."""

    interventional = True

    def __init__(self, step):
        self.step = step

    def inputs(self, features):
        return []

    def fit(self, frame, params, *, horizon):
        return {"rate": float(params["rate"])}

    def predict(self, state, history, horizon, *, levels=(), drivers=None, interventions=()):
        rows = []
        for uid, series in history.groupby("unique_id", sort=True):
            level, moment = float(series["y"].iloc[-1]), series["ds"].iloc[-1]
            for _ in range(horizon):
                moment = moment + self.step
                added = sum(
                    float(found.value)
                    for found in interventions
                    if found.target == "Site.inflow"
                    and found.start <= moment
                    and (found.end is None or moment < found.end)
                )
                level += state["rate"] + added
                rows.append({"unique_id": uid, "ds": moment, "mean": level})
        return pd.DataFrame(rows)

    def validity(self, frame):
        return {"y": (float(frame["y"].min()), float(frame["y"].max()))}


def drift(*, step, twin):
    return Drift(step)
'''


def level(sensor: str, hour: int) -> float:
    return round(BASE[sensor] + RISE * hour + CYCLE[hour % 4], 4)


def readings(hours: int = HOURS) -> str:
    lines = ["sensor_id,timestamp,level"]
    for hour in range(hours):
        stamp = (T0 + timedelta(hours=hour)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.extend(f"{sensor},{stamp},{level(sensor, hour)}" for sensor in BASE)
    return "\n".join(lines) + "\n"


def new_twin_id() -> str:
    return f"m5tk-{secrets.token_hex(4)}"


def m5_manifest(twin_id: str) -> dict[str, Any]:
    data = copy.deepcopy(manifest(twin_id))
    contract = data["contract"]
    contract["purpose"] = "Show and forecast the level of each sensor of the plant"
    level_question = contract["questions"]["q_level"]
    # Above the noise the profile measures in a level that swings with a four-hour cycle.
    level_question["tolerance"]["value"] = 2.0
    level_question["example"] = {"property": "Sensor.level"}
    contract["questions"]["q_ahead"] = {
        "text": "What will the level of each sensor be in two hours?",
        "kind": "predictive",
        "priority": 2,
        "tolerance": {"metric": "mae", "value": 1.0, "unit": "m"},
        "targets": ["Sensor.level"],
        "staleness": "P1D",
        "function": "f_forecast",
        "model": "level",
        "horizon": "PT2H",
    }
    contract["questions"]["q_whatif"] = {
        "text": "What would the levels be with more water let in?",
        "kind": "predictive",
        "priority": 3,
        "tolerance": {"metric": "mae", "value": 1000.0, "unit": "m"},
        "staleness": "P1D",
        "targets": ["Sensor.level"],
        "function": "f_whatif",
        "model": "whatif",
        "horizon": "PT2H",
    }
    contract["boundary"] = {
        "state": ["Sensor.level"],
        "controls": ["Site.inflow"],
        "observations": ["Sensor.level"],
    }
    data["ontology"] = {"path": "ontology/plant.yaml"}
    data["runtime"] = {
        "image": RUNTIME_IMAGE,
        "code": CODE_DIR,
        "worker_requirements": REQUIREMENTS_FILE,
    }
    # The grid of the twin's models: a static twin keeps it though it never syncs.
    data["sync"] = {"backfill_interval": "PT1H", "interval": "PT1H"}
    data["functions"]["f_forecast"] = {
        "description": "Forecast of the question's target by its selected model",
        "kind": "builtin",
        "entry": "forecast",
        "params": {"question": "q_ahead"},
        "complexity": "O(|V|)",
        "latency_budget": "PT60S",
    }
    data["functions"]["f_whatif"] = {
        "description": "Forecast of the levels by the twin's own drift model",
        "kind": "builtin",
        "entry": "forecast",
        "params": {"question": "q_whatif"},
        "complexity": "O(|V|)",
        "latency_budget": "PT60S",
    }
    data["functions"]["f_report"] = {
        "description": "A report of one sensor, from the twin's own code",
        "kind": "registered",
        "entry": "plant:report",
        "complexity": "O(1)",
        "latency_budget": "PT30S",
    }
    backtest = {"initial_window": "PT12H", "step": "PT2H", "min_folds": 3}
    data["models"] = {
        "level": {
            "question": "q_ahead",
            "target": "Sensor.level",
            "candidates": {
                "naive": {"family": "seasonal_naive", "params": dict(SEASON)},
                "theta": {"family": "theta", "params": dict(SEASON)},
            },
            "features": ["Sensor.level"],
            "backtest": dict(backtest),
            "acceptance": {"metric": "mae", "threshold": 0.5, "baseline": "seasonal"},
        },
        "whatif": {
            "question": "q_whatif",
            "target": "Sensor.level",
            "candidates": {"drift": {"family": "plant:drift", "params": {"rate": RISE}}},
            "features": ["Sensor.level"],
            "backtest": dict(backtest),
            "acceptance": {"metric": "mae", "threshold": 1000.0},
            "interventional": True,
        },
    }
    return data


# -- docker, by name only ------------------------------------------------------------


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def worker_images(twin_id: str) -> list[str]:
    """Tags of the worker images made for one twin."""
    done = _docker("image", "ls", "--format", "{{.Repository}}:{{.Tag}}", f"dta-worker-{twin_id}")
    return sorted(line for line in done.stdout.split() if line)


def remove_image(tag: str) -> None:
    assert tag.startswith(IMAGE_PREFIX), tag
    _docker("image", "rm", tag)


def remove_project(project: str) -> None:
    """Remove a stack of this kit with its volumes, by its compose project's name."""
    assert project.startswith(PROJECT_PREFIX), project
    if shutil.which("docker") is None:
        return
    _docker("compose", "--project-name", project, "down", "--volumes", "--remove-orphans")


def leftovers(project: str) -> dict[str, list[str]]:
    label = f"label=com.docker.compose.project={project}"

    def found(*args: str) -> list[str]:
        return [line for line in _docker(*args).stdout.split() if line]

    return {
        "containers": found("ps", "-aq", "--filter", label),
        "volumes": found("volume", "ls", "-q", "--filter", label),
        "networks": found("network", "ls", "-q", "--filter", label),
    }


class M5Twin(KitTwin):
    """The kit's plant as a twin that computes; its stack and images go afterwards."""

    def __init__(self, dta: Runner, base: Path) -> None:
        super().__init__(dta, base)
        self.id = new_twin_id()
        self.project = f"dta-{self.id}"
        (self.data / "readings.csv").write_text(readings(), encoding="utf-8")

    def write(self) -> None:
        self.ok("init", "--id", self.id)
        (self.root / "ontology").mkdir(exist_ok=True)
        (self.root / "ontology" / "plant.yaml").write_text(ONTOLOGY, encoding="utf-8")
        (self.root / CODE_DIR).mkdir(exist_ok=True)
        (self.root / CODE_DIR / "plant.py").write_text(PLANT_CODE, encoding="utf-8")
        (self.root / REQUIREMENTS_FILE).write_text(REQUIREMENT, encoding="utf-8")
        self.dta.write_manifest(m5_manifest(self.id))

    def resign(self) -> None:
        """Agree to the manifest again after a test changed it."""
        self.ok("manifest", "validate")
        self.ok("manifest", "confirm", "--all-user")
        self.ok("manifest", "validate")
        self.ok("manifest", "sign", "--non-interactive", "--approved-by", "Test Operator")

    def change_manifest(self, change: Any) -> None:
        data = self.dta.read_manifest()
        change(data)
        self.dta.write_manifest(data)
        self.resign()

    def build_record(self) -> dict[str, Any]:
        return json.loads((self.root / ".dta" / "build.json").read_text(encoding="utf-8"))

    def teardown(self) -> None:
        if (self.root / "compose.yaml").is_file() or (self.root / "stack").is_dir():
            self.dta.json("down", "--destroy-data")
        remove_project(self.project)
        for tag in worker_images(self.id):
            remove_image(tag)
        left = leftovers(self.project)
        assert not any(left.values()), left
        assert worker_images(self.id) == [], worker_images(self.id)


__all__ = [
    "DATA_ENV",
    "IMAGE_PREFIX",
    "M5Twin",
    "PROJECT_PREFIX",
    "level",
    "m5_manifest",
    "readings",
    "remove_image",
    "worker_images",
]
