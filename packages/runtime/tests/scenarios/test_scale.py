"""How long a scenario of a day takes: 96 steps of 15 minutes, two models of 20 series
each. Run with `-m scale`."""

from __future__ import annotations

import dataclasses
import os
import statistics
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
import pytest
from dta_core.log import Log
from dta_core.store import StoreSettings
from dta_runtime import scenarios
from dta_runtime.scenarios import ScenarioRequest
from dta_runtime.twin import Bundle, Twin, install_twin
from scenarios_testkit import (
    STEP,
    T0,
    artifact_row,
    at,
    commit,
    junction,
    manifest_for,
    model,
    model_row,
    node,
    prepared,
    register,
    tank,
)

pytestmark = pytest.mark.scale
log = Log("test.scale")

SERIES = 20
STEPS = 96
RUNS = 3
# Versions the live line went on by after the base, every object changing in each.
LIVE_AFTER = 8
TANKS = [f"T{index:02d}" for index in range(SERIES)]
JUNCTIONS = [f"J{index:02d}" for index in range(SERIES)]
TARGETS = ["Tank.level", "Junction.demand"]

# Fixed before anything was measured.
THRESHOLDS_S = {
    "run of 96 steps, two models of 20 series": 12.0,
    "compare with the live line, both targets": 1.5,
}
RUN, COMPARE = THRESHOLDS_S


@dataclass
class Plant:
    twin: Twin
    base: Any
    # label -> seconds of each run
    measured: dict[str, list[float]] = field(default_factory=dict)
    made: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture(scope="module")
def plant(
    server_settings: StoreSettings, _databases: list[str], water_ontology_text: str
) -> Iterator[Plant]:
    name = f"{server_settings.database}_scn_scale_{uuid.uuid4().hex[:8]}"
    _databases.append(name)
    installed = install_twin(
        dataclasses.replace(server_settings, database=name),
        Bundle(manifest=manifest_for(water_ontology_text), ontology_text=water_ontology_text),
        t=T0,
    )
    twin = installed.twin
    commit(
        twin,
        at(1),
        {
            "Tank": [tank(key, 5.0) for key in TANKS],
            "Junction": [junction(key, 10.0) for key in JUNCTIONS],
        },
    )
    state = "ab" * 32
    base = register(
        twin,
        {
            "TwinArtifact": [artifact_row(state, 1)],
            "TwinModel": [
                model_row("level", "persistence", "Tank.level", fitted=state),
                model_row("demand", "persistence", "Junction.demand", fitted=state),
            ],
        },
    )
    for step in range(1, LIVE_AFTER + 1):
        commit(
            twin,
            at(1 + step),
            {
                "Tank": [{"uid": node(key), "level": 5.0 + step / 10} for key in TANKS],
                "Junction": [{"uid": node(key), "demand": 10.0 + step} for key in JUNCTIONS],
            },
        )
    loaded = Plant(twin=twin, base=base)
    yield loaded
    twin.close()
    report = [
        f"{label}: slowest {max(runs):.3f} s, median {statistics.median(runs):.3f} s, "
        f"fastest {min(runs):.3f} s of {len(runs)} (threshold {THRESHOLDS_S[label]:.1f} s); "
        f"runs {', '.join(f'{run:.3f}' for run in runs)}"
        for label, runs in loaded.measured.items()
    ]
    one, five, fifteen = os.getloadavg()
    report.append(f"load average (1, 5, 15 min): {one:.2f} {five:.2f} {fifteen:.2f}")
    print("\n" + "\n".join(report))
    log.info(
        "scale measurements",
        **{label: [round(run, 3) for run in runs] for label, runs in loaded.measured.items()},
    )


def test_a_day_of_two_models_is_written_within_its_time(plant: Plant):
    # What the predictor answers is ready before it is asked: only the writing is timed.
    levels = prepared(
        {node(key): lambda j: 5.0 + (j % 24) / 10 for key in TANKS}, STEPS, plant.base.t
    )
    demands = prepared(
        {node(key): lambda j: 10.0 + (j % 12) for key in JUNCTIONS}, STEPS, plant.base.t
    )

    def predict(record: Any, **asked: Any) -> pa.Table:
        return levels if record["target"] == "Tank.level" else demands

    for index in range(RUNS):
        request = ScenarioRequest(
            name=f"a day ahead {index}",
            horizon=STEPS * STEP,
            models=[model("level"), model("demand")],
        )
        made = scenarios.create(plant.twin, request, at=plant.base, step=STEP)
        started = time.perf_counter()
        done = scenarios.run(plant.twin, made["uid"], predict=predict, budget_s=600.0)
        elapsed = time.perf_counter() - started
        plant.measured.setdefault(RUN, []).append(elapsed)
        plant.made.append(made)
        assert (done["status"], done["versions"]) == ("done", STEPS)
        head = scenarios.get(plant.twin, made["uid"])["head"]
        assert head == {"seq": plant.base.seq + STEPS, "t": plant.base.t + STEPS * STEP}
    assert max(plant.measured[RUN]) <= THRESHOLDS_S[RUN], plant.measured[RUN]


def test_a_day_is_compared_with_the_live_line_within_its_time(plant: Plant):
    assert plant.made, "the scenarios are made by the test before this one"
    uid = plant.made[-1]["uid"]
    for _ in range(RUNS):
        started = time.perf_counter()
        table = scenarios.compare(plant.twin, uid, targets=TARGETS)
        plant.measured.setdefault(COMPARE, []).append(time.perf_counter() - started)
        assert table.num_rows == 2 * SERIES * (STEPS + 1)
        assert table.column("scenario").null_count == 0
        # The live line is known for the base and the versions after it.
        assert table.column("against").null_count == 2 * SERIES * (STEPS - LIVE_AFTER)
    assert max(plant.measured[COMPARE]) <= THRESHOLDS_S[COMPARE], plant.measured[COMPARE]
