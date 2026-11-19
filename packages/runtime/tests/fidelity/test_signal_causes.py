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
from datetime import datetime, timedelta

import pyarrow as pa
from dta_runtime import fidelity
from dta_runtime.fidelity import reading
from dta_runtime.fidelity.records import signal_uid
from fidelity_testkit import TIME, Oracle, at, gauge, level, question

G1, G3 = gauge("G1"), gauge("G3")
LEVEL_G1 = signal_uid("Gauge.level", G1)
LEVEL_G3 = signal_uid("Gauge.level", G3)
SCHEMA = pa.schema(
    [
        ("unique_id", pa.string()),
        ("ds", TIME),
        ("mean", pa.float64()),
        ("lo_80", pa.float64()),
        ("hi_80", pa.float64()),
    ]
)


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
    """A forecaster of a series' own recent values, as the store holds them at the
    origin: the last value carried on along its last slope, three times as steep. Fed
    its own forecasts it runs away. Series other than `uids`, and origins before
    `since`, are predicted by the plant's rules."""

    def __init__(self, plant, uids: Sequence[str], since: datetime | None = None) -> None:
        self.plant = plant
        self.uids = set(uids)
        self.since = since or at(0)
        self.rules = Oracle(plant)
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
        table = self.rules(
            model,
            origin=origin,
            horizon=horizon,
            step=step,
            interventions=interventions,
            deadline_s=deadline_s,
        ).to_pylist()
        if origin.t < self.since:
            return pa.Table.from_pylist(table, schema=SCHEMA)
        store = self.plant.store
        before = reading.version_at(self.plant.twin, origin.t - step) or origin
        now = {row["uid"]: row["level"] for row in store.table("Gauge", at=origin).to_pylist()}
        then = {row["uid"]: row["level"] for row in store.table("Gauge", at=before).to_pylist()}
        rows = [row for row in table if row["unique_id"] not in self.uids]
        for uid in sorted(self.uids):
            self.origins.append((origin.t, now.get(uid)))
            last, previous = now.get(uid), then.get(uid)
            if last is None or previous is None:
                continue
            rows.extend(
                {
                    "unique_id": uid,
                    "ds": origin.t + j * step,
                    "mean": last + 3.0 * j * (last - previous),
                    "lo_80": None,
                    "hi_80": None,
                }
                for j in range(1, horizon + 1)
            )
        return pa.Table.from_pylist(rows, schema=SCHEMA)


def held(plant, uid: str, outcomes: Mapping[int, object]) -> list[float | None]:
    """The level of a gauge as each check left it."""
    return [
        row["level"]
        for outcome in outcomes.values()
        for row in plant.store.table(
            "Gauge", columns=["level"], filter=[("uid", "=", uid)], at=outcome.version
        ).to_pylist()
    ]


def test_an_inferred_value_never_rests_on_inferred_values(make_plant):
    # G3 is predicted from its own stored values from origin 14 on: off by more than the
    # tolerance, alone, it is called biased at check 16. Forecast from each check's
    # origin, its inferred values would be forecasts of forecasts.
    plant = make_plant()
    for i in range(1, 6):
        plant.sync(i)
    plant.activate("level", "Gauge.level", fitted_to=at(4))
    echo = Echo(plant, [G3], since=at(14))
    outcomes = plant.run(range(6, 31), echo)
    assert [found["object"] for i in range(6, 31) for found in outcomes[i].flagged] == [G3]
    assert outcomes[16].flagged
    # Made once, from step 15, the reading before the flagged one; refused there, as it
    # lies outside the range G3 was observed in: unknown from the flag on.
    signal = fidelity.signal(plant.twin, LEVEL_G3)
    inference = signal["detail"]["inference"]
    assert inference["origin"] == "2026-01-05T03:45:00Z"
    assert signal["detail"]["unknown"]["why"] == "out_of_range"
    values = held(plant, G3, {i: outcomes[i] for i in range(16, 31)})
    assert all(value is None for value in values)
    assert all(abs(value) < 100 for value in held(plant, G3, outcomes) if value is not None)


def test_an_inferred_value_rests_on_observed_values_and_a_moving_reading_clears(make_plant):
    # G1 sticks at 11.7 from step 22, a value it did not read before: forecast from the
    # stored values of each check's origin it would run away from there.
    plant = make_plant()
    stuck_steps = range(22, 34)
    plant.readings[("level", G1)] = lambda i: 11.7 if i in stuck_steps else level(G1, i)
    for i in range(1, 20):
        plant.sync(i)
    plant.activate("level", "Gauge.level", fitted_to=at(18))
    echo = Echo(plant, [G1])
    outcomes = plant.run(range(19, 41), echo)
    # Changed at step 22 for the last time: k = 22, p0 = 1/24, (1/24)^6 = 5.2e-9.
    assert [i for i, outcome in outcomes.items() if outcome.flagged] == [28]
    # Whatever the forecaster makes of it, the gauge holds an observed value, a value
    # inside the range it was observed in, or no value; never a runaway.
    assert all(value is None or 10.25 <= value <= 13.25 for value in held(plant, G1, outcomes))
    # The forecast that stood in for the readings was made from step 27, the reading
    # before the flagged one: an observed value, the value it was stuck at.
    inferred = [entry for i in range(28, 41) for entry in outcomes[i].check["signals"]["inferred"]]
    assert {(entry["origin"], entry["value"]) for entry in inferred} == {
        ("2026-01-05T06:45:00Z", 11.7)
    }
    assert (at(27), 11.7) in echo.origins
    # Readings move again from step 34: three changes by step 36, cleared there.
    assert [i for i, outcome in outcomes.items() if outcome.cleared] == [36]
    assert fidelity.signal(plant.twin, LEVEL_G1)["status"] == "ok"


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


def test_a_forecast_that_is_not_finite_leaves_the_value_unknown(make_plant):
    plant = make_plant()
    for i in range(1, 6):
        plant.sync(i)
    plant.activate("level", "Gauge.level", fitted_to=at(4))

    def offset(model, uid, origin):
        # G3 is predicted 1.8 too high from origin 14 (flagged at check 16), and not at
        # all from origin 15, the one its inferred value is forecast from.
        if uid != G3:
            return 0.0
        if origin == at(15):
            return math.nan
        return 1.8 if origin >= at(14) else 0.0

    outcomes = plant.run(range(6, 18), Oracle(plant, offset))
    assert [found["object"] for found in outcomes[16].flagged] == [G3]
    signal = fidelity.signal(plant.twin, LEVEL_G3)
    assert signal["detail"]["inference"]["values"] == {}
    assert signal["detail"]["unknown"]["why"] == "not_finite"
    assert [entry["signal"] for entry in outcomes[16].check["signals"]["unknown"]] == [LEVEL_G3]
    assert all(value is None for value in held(plant, G3, {16: outcomes[16], 17: outcomes[17]}))
