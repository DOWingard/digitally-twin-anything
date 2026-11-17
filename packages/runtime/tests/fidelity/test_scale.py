"""How long a check takes on a full-size plant, and the two reads on the path of every
action and sync run. Run with `-m scale`.

The plant: 50 gauges, 10 observed properties each (500 signals tested for being stuck),
3 questions of 20 series, a window of 96 intervals, a predictor that answers at once.
The checks before the timed ones fill the window with their records.
"""

from __future__ import annotations

import os
import statistics
import time

import pytest
from dta_core.log import Log
from dta_runtime import fidelity
from fidelity_testkit import GAUGE_PROPERTIES, TRUTH, Oracle, at, gauge, question, spec

pytestmark = pytest.mark.scale
log = Log("test.scale")

GAUGES = tuple(f"G{index:02d}" for index in range(1, 51))
SERIES = 20
WINDOW_STEPS = 96
# Steps synced and checked before the timed checks; the window is full by then.
WARM = 110
RUNS = 3
QUESTIONS = {
    "level": question("Gauge.level", 2, "level"),
    "flow": question("Gauge.flow", 2, "flow"),
    "r2": question("Gauge.r2", 4, "r2"),
}
MODELS = {qid: spec(qid, entry["targets"][0]) for qid, entry in QUESTIONS.items()}
STUCK = ("r9", gauge("G50"))

# Fixed before anything was measured.
THRESHOLDS_S = {"check": 3.0, "state": 0.05, "suspects": 0.05}


def test_a_full_size_check_and_the_reads_on_the_hot_path(make_plant):
    plant = make_plant(
        gauges=GAUGES,
        pumps=(),
        properties=GAUGE_PROPERTIES,
        observations=[f"Gauge.{name}" for name in GAUGE_PROPERTIES],
        questions=QUESTIONS,
        models=MODELS,
        window_steps=WINDOW_STEPS,
    )
    # One signal goes stuck, so that a suspect signal is held and estimated throughout.
    name, uid = STUCK
    plant.readings[STUCK] = lambda i: 7.0 if i >= WARM - 10 else TRUTH[name](uid, i)
    for i in range(1, 6):
        plant.sync(i)
    for qid, entry in QUESTIONS.items():
        plant.activate(qid, entry["targets"][0], fitted_to=at(4))
    oracle = Oracle(plant, series=plant.gauges[:SERIES])
    warm_started = time.perf_counter()
    plant.run(range(6, WARM + 1), oracle)
    warm_s = time.perf_counter() - warm_started
    assert fidelity.suspects(plant.twin) == {("Gauge.r9", STUCK[1])}

    measured: dict[str, list[float]] = {}
    for run in range(RUNS):
        i = WARM + 1 + run
        plant.sync(i)
        started = time.perf_counter()
        outcome = plant.check(i, oracle)
        measured.setdefault("check", []).append(time.perf_counter() - started)
        entries = outcome.check["questions"]
        assert all(entries[qid]["scored"] for qid in QUESTIONS), entries
        assert all(entries[qid]["window"]["n"] >= WINDOW_STEPS - 5 for qid in QUESTIONS)
        assert outcome.check["signals"]["tested"]["stuck"] == 10 * len(GAUGES) - 1
    for name, read in (("state", fidelity.state), ("suspects", fidelity.suspects)):
        for _ in range(RUNS):
            started = time.perf_counter()
            read(plant.twin)
            measured.setdefault(name, []).append(time.perf_counter() - started)

    report = [
        f"{label}: slowest {max(runs) * 1000:.1f} ms, median {statistics.median(runs) * 1000:.1f}"
        f" ms (threshold {THRESHOLDS_S[label] * 1000:.0f} ms); runs "
        + ", ".join(f"{run * 1000:.1f}" for run in runs)
        for label, runs in measured.items()
    ]
    one, five, fifteen = os.getloadavg()
    report.append(f"warm-up: {WARM - 5} syncs and checks in {warm_s:.1f} s")
    report.append(f"load average (1, 5, 15 min): {one:.2f} {five:.2f} {fifteen:.2f}")
    print("\n" + "\n".join(report))
    log.info(
        "fidelity scale measurements",
        **{label: [round(run * 1000, 1) for run in runs] for label, runs in measured.items()},
    )
    for label, runs in measured.items():
        assert max(runs) <= THRESHOLDS_S[label], (label, runs)
