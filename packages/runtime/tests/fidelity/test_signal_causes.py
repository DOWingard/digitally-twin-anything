"""How a suspect signal went wrong on a live twin, reproduced over a real store, and what
holds now.

1. A level moving normally was called stuck: it is flat for a few intervals at each
   turning point, and taken as independent draws such a flat is improbable. A smooth
   signal is not tested for being stuck any more.
2. A stuck signal whose readings moved again was never cleared: clearing compared the
   raw reading with an estimate that had drifted away. It clears now when its raw
   readings change again.
3. The inferred value was forecast at every check from an origin whose value of the
   same series was itself inferred: a model of the series' own recent values, fed its
   own output, runs away. It is forecast once now, from what was observed, and is
   unknown once that forecast's lead passes the question's horizon.
4. A biased signal cleared when there was no estimate to compare its reading with. A
   check with nothing to compare with neither counts nor resets now.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import timedelta

import pyarrow as pa
from dta_runtime import fidelity
from dta_runtime.fidelity import reading
from dta_runtime.fidelity.records import signal_uid
from fidelity_testkit import (
    TIME,
    Oracle,
    at,
    gauge,
    level,
    question,
)

G1, G3 = gauge("G1"), gauge("G3")
LEVEL_G1 = signal_uid("Gauge.level", G1)
LEVEL_G3 = signal_uid("Gauge.level", G3)


def smooth_level(i: int) -> float:
    """A level of 15-minute readings to 0.01: a daily swing of 0.5 m that peaks at step
    40, flat there for a few readings."""
    return round(10.0 + 0.5 * math.sin(2 * math.pi * (i - 16) / 96), 2)


def old_rule(readings: Sequence[float], end: int) -> bool:
    """What the stuck test called stuck before: the intervals up to step `end` as
    independent draws, p0 = (u + 1)/(k + 2), stuck when n >= 3 and p0^n < 1e-4."""
    changed = [readings[i] != readings[i - 1] for i in range(2, end + 1)]
    last = max(index for index, flag in enumerate(changed) if flag)
    k, n = last + 1, len(changed) - last - 1
    u = k - sum(changed[:k])
    return k - u >= 20 and n >= 3 and ((u + 1) / (k + 2)) ** n < 1e-4


def test_a_level_flat_at_its_turning_point_is_not_called_stuck(make_plant):
    plant = make_plant(questions={}, models={})
    plant.readings[("level", G1)] = smooth_level
    readings = [smooth_level(i) for i in range(0, 61)]
    # The flat at the peak: steps 38 to 42 read 10.5. The old test called it stuck.
    assert readings[38:43] == [10.5] * 5
    assert any(old_rule(readings, end) for end in range(30, 61))
    for i in range(1, 30):
        plant.sync(i)
    outcomes = plant.run(range(30, 61), Oracle(plant))
    assert all(outcome.flagged == [] for outcome in outcomes.values())
    assert fidelity.signals(plant.twin) == []


class Echo:
    """A forecaster of the series' own recent values, as the store holds them at the
    origin: the last value carried on along its last slope, three times as steep. Fed
    its own forecasts it runs away."""

    def __init__(self, plant) -> None:
        self.plant = plant
        self.origins: list[tuple] = []

    def __call__(
        self,
        model: Mapping[str, object],
        *,
        origin,
        horizon: int,
        step: timedelta,
        interventions: Sequence[object],
        deadline_s: float,
    ) -> pa.Table:
        store = self.plant.store
        before = reading.version_at(self.plant.twin, origin.t - step) or origin
        now = {row["uid"]: row["level"] for row in store.table("Gauge", at=origin).to_pylist()}
        then = {row["uid"]: row["level"] for row in store.table("Gauge", at=before).to_pylist()}
        self.origins.append((origin.t, now.get(G1)))
        uids, times, means = [], [], []
        for uid in sorted(now):
            last, previous = now[uid], then.get(uid)
            if last is None or previous is None:
                continue
            for j in range(1, horizon + 1):
                uids.append(uid)
                times.append(origin.t + j * step)
                means.append(last + 3.0 * j * (last - previous))
        return pa.table(
            {
                "unique_id": pa.array(uids, pa.string()),
                "ds": pa.array(times, TIME),
                "mean": pa.array(means, pa.float64()),
            }
        )


def test_an_inferred_value_rests_on_observed_values_and_a_moving_reading_clears(make_plant):
    plant = make_plant()
    stuck_steps = range(22, 34)
    plant.readings[("level", G1)] = lambda i: level(G1, 21) if i in stuck_steps else level(G1, i)
    for i in range(1, 20):
        plant.sync(i)
    plant.activate("level", "Gauge.level", fitted_to=at(18))
    echo = Echo(plant)
    outcomes = plant.run(range(19, 41), echo)
    flagged_at = next(i for i, outcome in outcomes.items() if outcome.flagged)
    assert flagged_at == 27
    # Whatever the forecaster makes of it, the gauge holds an observed value, a value
    # inside the range it was observed in, or no value; never a runaway.
    held = [
        row["level"]
        for i in range(19, 41)
        for row in plant.store.table(
            "Gauge", columns=["level"], filter=[("uid", "=", G1)], at=outcomes[i].version
        ).to_pylist()
    ]
    assert all(value is None or 10.25 <= value <= 13.25 for value in held)
    # The forecast that stood in for the readings was made from step 26, the reading
    # before the flagged one: an observed value, the value it was stuck at.
    signal = fidelity.signal(plant.twin, LEVEL_G1)
    assert signal["detail"]["episodes"][0]["since"] == "2026-01-05T06:45:00Z"
    inferred = [entry for i in range(27, 41) for entry in outcomes[i].check["signals"]["inferred"]]
    assert inferred and {entry["origin"] for entry in inferred} == {"2026-01-05T06:30:00Z"}
    assert (at(26), level(G1, 21)) in echo.origins
    # Readings move again from step 34: three changes by step 36, cleared there.
    cleared_at = next(i for i, outcome in outcomes.items() if outcome.cleared)
    assert cleared_at == 36
    assert signal["status"] == "ok"


def test_a_biased_signal_clears_on_agreement_with_its_inferred_value(make_plant):
    plant = make_plant(questions={"level": question("Gauge.level", 4, "level")})
    for i in range(1, 6):
        plant.sync(i)
    plant.activate("level", "Gauge.level", fitted_to=at(4))

    def once(model, uid, origin):
        # Origin 14 alone is predicted 1.8 too high for G3: scored at check 18.
        return 1.8 if uid == G3 and origin == at(14) else 0.0

    outcomes = plant.run(range(6, 23), Oracle(plant, once))
    assert [i for i, outcome in outcomes.items() if outcome.flagged] == [18]
    # The forecast made at the flag, from step 17, agrees with the readings at leads
    # 2, 3 and 4: three sound checks, within the horizon of 4.
    counts = [fidelity.signal(plant.twin, LEVEL_G3, at=outcomes[i].version) for i in (19, 20)]
    assert [found["clear_checks"] for found in counts] == [1, 2]
    assert [found["uid"] for found in outcomes[21].cleared] == [LEVEL_G3]
    steps = next(found for found in fidelity.incidents(plant.twin) if found["subject"] == LEVEL_G3)[
        "steps"
    ]
    assert steps[-1]["action"] == "cleared" and steps[-1]["tolerance"] == 0.5


def test_a_biased_signal_without_anything_to_compare_with_is_not_cleared(make_plant):
    plant = make_plant()
    for i in range(1, 6):
        plant.sync(i)
    model = plant.activate("level", "Gauge.level", fitted_to=at(4))
    oracle = Oracle(
        plant, lambda model, uid, origin: 1.8 if uid == G3 and origin >= at(14) else 0.0
    )
    outcomes = plant.run(range(6, 17), oracle)
    assert [found["object"] for found in outcomes[16].flagged] == [G3]
    # A refit to data ahead of every origin: the question scores nothing, and past the
    # horizon of the forecast made at the flag there is no value to compare with.
    plant.refit(model, fitted_to=at(40))
    later = plant.run(range(17, 26), oracle)
    assert all(outcome.cleared == [] for outcome in later.values())
    signal = fidelity.signal(plant.twin, LEVEL_G3)
    assert signal["status"] == "suspect" and signal["clear_checks"] == 0
    # The forecast 1.8 too high leaves the range G3 was observed in (13 to 14.5, widened
    # by half its width) at lead 2: from then on its value is unknown.
    assert signal["detail"]["unknown"]["why"] == "out_of_range"
    assert signal["detail"]["inference"]["range"] == {
        "low": 12.25,
        "high": 15.25,
        "declared": {"low": False, "high": False},
        "observed": [13.0, 14.5],
        "margin": 0.75,
    }
