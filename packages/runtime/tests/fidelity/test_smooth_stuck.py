"""A smooth signal that stops, over a real store: the length of its run cannot tell a
frozen sensor from a level held at a turning point, the twin's own forecast can.

The level of G1 swings 2 m a day in readings to 0.01 m; the level question (ten steps
ahead, mae, ε 0.5 m, so a margin of 0.25 m) would infer it. Its forecaster knows the
level's path. Frozen at step 52 while the level falls 0.1 m an interval, it lies 0.30 m
from the forecast at the third interval of its run, and is called stuck there.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

import pyarrow as pa
import pytest
from dta_core.store.model import iso_utc
from dta_runtime import fidelity
from dta_runtime.fidelity.records import signal_uid
from fidelity_testkit import TIME, Oracle, at, gauge, index_of, question

G1 = gauge("G1")
LEVEL_G1 = signal_uid("Gauge.level", G1)
HORIZON = 10
FROZEN_AT = 52
SCHEMA = pa.schema(
    [
        ("unique_id", pa.string()),
        ("ds", TIME),
        ("mean", pa.float64()),
        ("lo_80", pa.float64()),
        ("hi_80", pa.float64()),
    ]
)


def swing(i: int) -> float:
    """Peaks at step 40 (12.0), falls fastest at 64, by 0.13 m an interval."""
    return round(10.0 + 2.0 * math.sin(2 * math.pi * (i - 16) / 96), 2)


def frozen(until: int) -> Callable[[int], float]:
    """Reads swing's value of step 52 from 53 to `until`, then swing again."""
    return lambda i: swing(FROZEN_AT) if FROZEN_AT < i <= until else swing(i)


def held_from(start: int, steps: int) -> Callable[[int], float]:
    """The level held at its value of `start` for `steps` intervals, then rising on as
    before, as a level does while what fills it stops."""

    def read(i: int) -> float:
        if i <= start:
            return swing(i)
        return swing(start) if i <= start + steps else swing(i - steps)

    return read


class Knows:
    """The level of G1 along `path`, every other series by the plant's rules; refuses
    every call when told to. Keeps every call (the plant's oracle's `calls`)."""

    def __init__(self, plant, path: Callable[[int], float], *, refuse: bool = False) -> None:
        self.rules = Oracle(plant, refuse=refuse)
        self.path = path

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.rules.calls

    def __call__(self, model: Mapping[str, Any], **kwargs: Any) -> pa.Table:
        rows = self.rules(model, **kwargs).to_pylist()
        if model["target"] == "Gauge.level":
            for row in rows:
                if row["unique_id"] == G1:
                    row["mean"] = self.path(index_of(row["ds"]))
        return pa.Table.from_pylist(rows, schema=SCHEMA)


def level_question(**options: Any) -> dict[str, Any]:
    return {"level": question("Gauge.level", HORIZON, "level", **options)}


def started(make_plant, read: Callable[[int], float], **options: Any):
    plant = make_plant(**options)
    plant.readings[("level", G1)] = read
    for i in range(1, 20):
        plant.sync(i)
    if options.get("questions") != {}:
        plant.activate("level", "Gauge.level", fitted_to=at(18))
    return plant


def entry_of(outcome) -> dict[str, Any] | None:
    """The check's forecast test of G1's level, if it made one."""
    found = [
        entry for entry in outcome.check["signals"]["forecasts"] if entry["signal"] == LEVEL_G1
    ]
    return found[0] if found else None


def calls_at(knows: Knows, step: int) -> int:
    return sum(1 for call in knows.calls if call["origin"] == at(step))


def stored(plant, outcome) -> float | None:
    (row,) = plant.store.table(
        "Gauge", columns=["level"], filter=[("uid", "=", G1)], at=outcome.version
    ).to_pylist()
    return row["level"]


def test_a_smooth_level_frozen_mid_slope_is_flagged_and_clears_when_it_moves_again(make_plant):
    plant = started(make_plant, frozen(until=55), questions=level_question())
    knows = Knows(plant, swing)
    outcomes = plant.run(range(19, 60), knows)
    # Two intervals into the run nothing is tested; at the third the frozen 11.41 lies
    # 0.30 from the forecast 11.11, more than half the tolerance.
    assert [step for step, outcome in outcomes.items() if outcome.flagged] == [55]
    assert entry_of(outcomes[54]) is None
    entry = entry_of(outcomes[55])
    assert {name: entry[name] for name in ("question", "origin", "lead", "refused", "stuck")} == {
        "question": "level",
        "origin": iso_utc(at(FROZEN_AT)),
        "lead": 3,
        "refused": None,
        "stuck": True,
    }
    assert (entry["value"], entry["reading"], entry["margin"]) == (11.11, 11.41, 0.25)
    (flag,) = outcomes[55].flagged
    assert flag["uid"] == LEVEL_G1
    signal = fidelity.signal(plant.twin, LEVEL_G1)
    test = signal["detail"]["test"]
    assert (test["model"], test["tested"], test["stuck"], test["skipped"]) == (
        "forecast",
        True,
        True,
        None,
    )
    assert test["forecast"]["lead"] == 3
    # Its value is that forecast, read on at the lead the run has reached: no new call.
    inference = signal["detail"]["inference"]
    assert inference["origin"] == entry["origin"]
    assert inference["values"] == entry["inference"]["values"]
    assert [stored(plant, outcomes[step]) for step in (55, 56)] == [swing(55), swing(56)]
    assert calls_at(knows, FROZEN_AT) == 1
    # It reads 56, 57, 58 again: three changes since the flag, no run in progress.
    assert outcomes[58].check["signals"]["cleared"] == [LEVEL_G1]
    assert outcomes[58].check["signals"]["suspect"] == []
    # The sync after the clearing writes its readings on the object again.
    assert stored(plant, outcomes[59]) == swing(59)


def test_a_level_held_as_its_forecast_says_is_never_flagged_and_forecast_once(make_plant):
    read = held_from(30, 8)
    plant = started(make_plant, read, questions=level_question())
    knows = Knows(plant, read)
    outcomes = plant.run(range(19, 40), knows)
    assert all(outcome.flagged == [] for outcome in outcomes.values())
    for step in range(33, 39):
        entry = entry_of(outcomes[step])
        assert (entry["lead"], entry["stuck"], entry["refused"]) == (step - 30, False, None)
        assert entry["value"] == entry["reading"] == swing(30)
    assert entry_of(outcomes[39]) is None
    # Six checks of the run read one forecast (the level question scores origin 30 only
    # at check 40).
    assert calls_at(knows, 30) == 1
    assert fidelity.signals(plant.twin) == []


def test_a_frozen_smooth_level_no_question_infers_is_not_tested(make_plant):
    plant = started(make_plant, frozen(until=58), questions={}, models={})
    outcomes = plant.run(range(19, 59), Oracle(plant))
    assert all(outcome.flagged == [] for outcome in outcomes.values())
    entry = entry_of(outcomes[57])
    assert (entry["question"], entry["stuck"], entry["refused"]["code"]) == (
        None,
        False,
        "no_question",
    )
    assert entry["inference"] is None


def test_a_refused_forecast_flags_nothing_says_why_and_is_asked_once(make_plant):
    plant = started(make_plant, frozen(until=60), questions=level_question())
    knows = Knows(plant, swing, refuse=True)
    outcomes = plant.run(range(19, 62), knows)
    assert all(outcome.flagged == [] for outcome in outcomes.values())
    for step in range(55, 61):
        entry = entry_of(outcomes[step])
        assert (entry["stuck"], entry["value"], entry["refused"]["code"]) == (
            False,
            None,
            "predictor_refused",
        )
        assert "worker.unavailable" in entry["refused"]["message"]
    assert calls_at(knows, FROZEN_AT) == 1


def test_a_relative_metric_gives_a_margin_relative_to_the_reading(make_plant):
    plant = started(
        make_plant, frozen(until=55), questions=level_question(metric="mape", tolerance=0.05)
    )
    outcomes = plant.run(range(19, 56), Knows(plant, swing))
    entry = entry_of(outcomes[55])
    assert entry["margin"] == pytest.approx(0.025 * 11.41)
    assert abs(entry["value"] - entry["reading"]) > entry["margin"]
    assert [step for step, outcome in outcomes.items() if outcome.flagged] == [55]
