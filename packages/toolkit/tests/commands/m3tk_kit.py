"""A small twin for the command tests: two types, CSV files, hourly readings with history.

The twin's ids start with `m3tk-` and its host ports lie in 18800-18899, so its
stack never collides with another one on this machine.

A live variant reads one reading per sensor and minute, for the last three
hours before the test starts; the test data is made from the wall clock, the
twin itself only ever reads the times in the data.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import shutil
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import pytest
import yaml

# The image the stacks of these tests run. A tree whose code differs from the one the
# default tag was built from builds its own image and names it in this variable, so
# that the toolkit under test and the services it starts are of one version.
RUNTIME_IMAGE = os.environ.get("DTA_TEST_RUNTIME_IMAGE") or "dta-runtime:0.1.0"
PROJECT_PREFIX = "dta-m3tk-"
PORTS = range(18800, 18900)
DATA_ENV = "M3TK_DATA_DIR"

ONTOLOGY = """\
id: https://example.org/dta/m3tk-plant
name: m3tk_plant
description: Sites and the level sensors installed at them.
prefixes:
  linkml: https://w3id.org/linkml/
  plant: https://example.org/dta/m3tk-plant/
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

# One more object type: changes the type signature the user confirmed.
EXTRA_TYPE = """
  Gauge:
    description: A gauge read by hand.
    attributes:
      uid:
        identifier: true
        required: true
"""

SITES = "site_id,name\nnorth,North works\nsouth,South works\n"
SENSORS = "sensor_id,site_id,kind\nn1,north,level\nn2,north,level\ns1,south,level\n"
_HOURS = ("00", "01", "02", "03")
_LEVELS = {"n1": (1.0, 1.5, 2.0, 1.8), "n2": (0.5, 0.6, 0.7, 0.9), "s1": (3.0, 2.5, 2.2, 2.4)}


def readings(bad_row: bool = False) -> str:
    lines = ["sensor_id,timestamp,level"]
    for index, hour in enumerate(_HOURS):
        for sensor, levels in _LEVELS.items():
            level = levels[index]
            if bad_row and sensor == "s1" and index == 2:
                level = -1.0  # below the ontology's minimum: the row is set aside
            lines.append(f"{sensor},2026-01-01T{hour}:00:00Z,{level}")
    return "\n".join(lines) + "\n"


def write_sources(folder: Path, bad_row: bool = False, *, text: str | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "sites.csv").write_text(SITES, encoding="utf-8")
    (folder / "sensors.csv").write_text(SENSORS, encoding="utf-8")
    (folder / "readings.csv").write_text(
        text if text is not None else readings(bad_row), encoding="utf-8"
    )
    return folder


# -- the live variant ---------------------------------------------------------------

LIVE_MINUTES = 180
STAMP = "%Y-%m-%dT%H:%M:%SZ"


def minute_floor(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


def _live_level(sensor: str, step: int) -> float:
    # A level that swings every minute: profiling measures a change time of one
    # reading, so the derived interval is the source's minute. The slow rise makes
    # each hour's last reading differ from the one before, so the hourly versions of
    # the load hold a history.
    return round(_LEVELS[sensor][0] + (0.3 if step % 2 else 0.0) + 0.001 * step, 3)


def live_readings(end: datetime, minutes: int = LIVE_MINUTES) -> str:
    """One reading per sensor and minute for the `minutes` minutes before `end`."""
    start = minute_floor(end) - timedelta(minutes=minutes)
    lines = ["sensor_id,timestamp,level"]
    for step in range(minutes):
        at = start + timedelta(minutes=step)
        for sensor in _LEVELS:
            lines.append(f"{sensor},{at.strftime(STAMP)},{_live_level(sensor, step)}")
    return "\n".join(lines) + "\n"


def live_manifest(twin_id: str, *, ui: bool = False) -> dict[str, Any]:
    """The kit's twin, kept in step with its readings; `sync.interval` is derived later."""
    data = copy.deepcopy(manifest(twin_id, ui=ui))
    contract = data["contract"]
    contract["tier"] = "live"
    contract["notification"] = {"kind": "log"}
    contract["questions"]["q_level"]["staleness"] = "PT1H"
    # Above the noise floor profiling measures in a level that swings every minute.
    contract["questions"]["q_level"]["tolerance"]["value"] = 1.0
    data["fidelity"] = {"interval": "PT1H", "window": "P1D"}
    return data


def new_twin_id() -> str:
    return f"m3tk-{secrets.token_hex(4)}"


def manifest(twin_id: str, *, ui: bool = False) -> dict[str, Any]:
    return {
        "twin": {"id": twin_id, "name": "Plant levels (test)"},
        "contract": {
            "purpose": "Show the level of each sensor of the plant and how it changed",
            "tier": "static",
            "questions": {
                "q_level": {
                    "text": "What was the level of each sensor at a given time?",
                    "kind": "descriptive",
                    "priority": 1,
                    "tolerance": {"metric": "mae", "value": 0.1, "unit": "m"},
                    "staleness": "P1D",
                    "targets": ["Sensor.level"],
                    "function": "f_latest",
                }
            },
            "boundary": {"state": ["Sensor.level"], "observations": ["Sensor.level"]},
        },
        "ontology": {"path": "ontology/plant.yaml"},
        "runtime": {"image": RUNTIME_IMAGE},
        "sources": {
            "plant": {
                "description": "CSV files the plant exports",
                "connector": "file",
                "location": f"file://${{{DATA_ENV}}}",
                "env": [DATA_ENV],
                "options": {
                    "datasets": {
                        "sites": {"glob": "sites.csv", "format": "csv"},
                        "sensors": {"glob": "sensors.csv", "format": "csv"},
                        "readings": {"glob": "readings.csv", "format": "csv"},
                    }
                },
                "mappings": {
                    "sites": {
                        "dataset": "sites",
                        "object_type": "Site",
                        "identity": ["site_id"],
                        "properties": {"name": {"column": "name"}},
                    },
                    "sensors": {
                        "dataset": "sensors",
                        "object_type": "Sensor",
                        "identity": ["sensor_id"],
                        "properties": {"site": {"column": "site_id"}, "kind": {"column": "kind"}},
                    },
                    "levels": {
                        "dataset": "readings",
                        "object_type": "Sensor",
                        "identity": ["sensor_id"],
                        "valid_time": "timestamp",
                        "properties": {"level": {"column": "level", "unit": "m"}},
                    },
                },
            }
        },
        "sync": {"backfill_interval": "PT1H"},
        "functions": {
            "f_latest": {
                "description": "Latest value of a property",
                "kind": "builtin",
                "entry": "latest",
                "complexity": "O(|V|)",
                "latency_budget": "PT1S",
            }
        },
        "interfaces": {"ui": ui},
        "env": {
            DATA_ENV: {"description": "Folder holding the plant's CSV files", "secret": False},
            "DTA_BASE_URL": {"description": "Twin API address", "secret": False},
            "DTA_API_TOKEN": {"description": "Twin API token"},
            "DTA_API_PORT": {"description": "Twin API port on the host", "secret": False},
            "DTA_UI_PORT": {"description": "Twin UI port on the host", "secret": False},
        },
    }


# -- docker -------------------------------------------------------------------


def _image_present(name: str) -> bool:
    if shutil.which("docker") is None:
        return False
    done = subprocess.run(["docker", "image", "inspect", name], capture_output=True, check=False)
    return done.returncode == 0


needs_stack = pytest.mark.skipif(
    not _image_present(RUNTIME_IMAGE),
    reason=f"needs docker and the image {RUNTIME_IMAGE}, built from docker/runtime.Dockerfile",
)


def free_port(taken: set[int] | None = None) -> int:
    """A port of the assigned range that nothing listens on now."""
    for port in secrets.SystemRandom().sample(list(PORTS), len(PORTS)):
        if taken and port in taken:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("no free port in 18800-18899")


def _docker_ids(*args: str) -> list[str]:
    done = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    return [line for line in done.stdout.split() if line]


def remove_project(project: str) -> None:
    """Remove every container, volume and network of one of this kit's stacks."""
    assert project.startswith(PROJECT_PREFIX), project
    if shutil.which("docker") is None:
        return
    label = f"label=com.docker.compose.project={project}"
    containers = _docker_ids("ps", "-aq", "--filter", label)
    if containers:
        subprocess.run(["docker", "rm", "-f", *containers], capture_output=True, check=False)
    volumes = _docker_ids("volume", "ls", "-q", "--filter", label)
    if volumes:
        subprocess.run(["docker", "volume", "rm", *volumes], capture_output=True, check=False)
    networks = _docker_ids("network", "ls", "-q", "--filter", label)
    if networks:
        subprocess.run(["docker", "network", "rm", *networks], capture_output=True, check=False)


def leftovers(project: str) -> dict[str, list[str]]:
    label = f"label=com.docker.compose.project={project}"
    return {
        "containers": _docker_ids("ps", "-aq", "--filter", label),
        "volumes": _docker_ids("volume", "ls", "-q", "--filter", label),
        "networks": _docker_ids("network", "ls", "-q", "--filter", label),
    }


# -- one test twin ---------------------------------------------------------------


class Runner(Protocol):
    root: Path

    def json(self, *args: str, input: str | None = None) -> tuple[int, dict[str, Any]]: ...


SECRET_MIN = 6  # shorter values (ports) are not masked by the toolkit either


class KitTwin:
    """One test twin: workspace, CSV files, host ports and its stack, removed afterwards.

    Every JSON document a command printed is kept, so a test can check at the
    end that no value of `.env` ever reached the output.
    """

    def __init__(
        self,
        dta: Runner,
        base: Path,
        *,
        ui: bool = False,
        bad_row: bool = False,
        live: bool = False,
        readings_end: datetime | None = None,
    ):
        self.dta = dta
        self.root = dta.root
        self.id = new_twin_id()
        self.project = f"dta-{self.id}"  # the stack's compose project
        self.ui = ui
        self.live = live
        text = None
        if live:
            text = live_readings(readings_end or datetime.now(UTC))
        self.data = write_sources(base / "data", bad_row=bad_row, text=text)
        self.outputs: list[str] = []

    def append_readings(self, at: datetime) -> str:
        """One more reading per sensor at `at`, as the plant's export would add them."""
        stamp = at.astimezone(UTC).replace(microsecond=0).strftime(STAMP)
        rows = "".join(f"{sensor},{stamp},1.234\n" for sensor in _LEVELS)
        with (self.data / "readings.csv").open("a", encoding="utf-8") as handle:
            handle.write(rows)
        return stamp

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    def run(self, *args: str) -> tuple[int, dict[str, Any]]:
        code, document = self.dta.json(*args)
        self.outputs.append(json.dumps(document))
        return code, document

    def ok(self, *args: str) -> dict[str, Any]:
        code, document = self.run(*args)
        assert code == 0, document
        return document["result"]

    def failed(self, code: str, *args: str, exit_code: int = 1) -> dict[str, Any]:
        exit_, document = self.run(*args)
        assert exit_ == exit_code, document
        assert document["error"]["code"] == code, document
        return document["error"]

    def gates(self) -> dict[str, str]:
        result = self.ok("status")
        return {gate["name"]: gate["result"] for gate in result["gates"]}

    def write(self) -> None:
        """init, then the ontology file and the manifest as the user and agent wrote them."""
        self.ok("init", "--id", self.id)
        (self.root / "ontology").mkdir(exist_ok=True)
        (self.root / "ontology" / "plant.yaml").write_text(ONTOLOGY, encoding="utf-8")
        written = live_manifest(self.id, ui=self.ui) if self.live else manifest(self.id, ui=self.ui)
        (self.root / "manifest.yaml").write_text(
            yaml.safe_dump(written, sort_keys=False), encoding="utf-8"
        )

    def fill_env(self) -> int:
        """Set the names `env template` left empty; returns the API port."""
        api_port = free_port()
        values = {
            "DTA_API_PORT": str(api_port),
            DATA_ENV: str(self.data),
            "DTA_BASE_URL": f"http://127.0.0.1:{api_port}",
            "DTA_UI_PORT": str(free_port({api_port})),
        }
        lines = []
        for line in self.env_file.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name in values and not value:
                line = f"{name}={values[name]}"
            lines.append(line)
        self.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")  # keeps mode 0600
        return api_port

    def env_values(self) -> list[str]:
        """Values of `.env` long enough that the toolkit must never show them."""
        if not self.env_file.is_file():
            return []
        values = []
        for line in self.env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            value = line.split("=", 1)[1]
            if len(value) >= SECRET_MIN:
                values.append(value)
        return values

    def prepare(self) -> None:
        """Every step before `build`, each expected to pass."""
        self.write()
        self.ok("ontology", "check", "--write")
        self.ok("env", "template")
        self.fill_env()
        self.ok("source", "probe")
        self.ok("profile")
        if self.live:
            self.ok("sync", "derive", "--write")
        self.ok("manifest", "validate")
        self.ok("manifest", "confirm", "--all-user")
        self.ok("manifest", "validate")
        self.ok("manifest", "sign", "--non-interactive", "--approved-by", "Test Operator")

    def leaked(self, extra: list[str] | None = None) -> list[str]:
        """Values of `.env` found in any printed document or in the given texts."""
        texts = self.outputs + (extra or [])
        return sorted({v for v in self.env_values() for text in texts if v in text})

    def teardown(self) -> None:
        if (self.root / "compose.yaml").is_file():
            self.dta.json("down", "--destroy-data")
        remove_project(self.project)
        left = leftovers(self.project)
        assert not any(left.values()), left
