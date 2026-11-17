"""For any interventions on a grid: after a run, what the branch holds for an assumed
property at each grid time is what the request says is in force then.

The expectation is worked out here from the request alone, the plain way: an
intervention is in force at a grid time that is at or after its start and before
its end; where none is, the property holds what it held at the base.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pyarrow as pa
from dta_core.store.model import iso_utc
from dta_runtime import scenarios
from hypothesis import given, settings
from hypothesis import strategies as st
from scenarios_testkit import STEP, World, cells, create, link, node, prepared

STEPS = 6
MINUTES = STEPS * 15
# (target, type, property, uid, value at the base): properties no model of the tests
# predicts and the manifest does not call a control.
PROPERTIES = [
    ("Valve.setting", "Valve", "setting", link("V1"), 1.0),
    ("Valve.setting", "Valve", "setting", link("V2"), 2.0),
    # Named through the class above its type.
    ("Node.elevation", "Junction", "elevation", node("J2"), 40.0),
    ("Junction.base_demand", "Junction", "base_demand", node("J1"), 5.0),
    ("Pipe.roughness", "Pipe", "roughness", link("P1"), 0.5),
]
VALUES = [0.25, 3.0, 4.5, 8.0]
# Minutes after the base: on the grid as often as off it.
minute = st.one_of(
    st.sampled_from(range(0, MINUTES + 1, 15)), st.integers(min_value=0, max_value=MINUTES)
)


@st.composite
def spans(draw: st.DrawFn) -> list[tuple[int, int | None, float]]:
    """Interventions on one property that do not overlap: (start, end, value) in minutes
    after the base; some follow one another without a gap, the last may have no end."""
    cuts = sorted(draw(st.sets(minute, max_size=6)))
    found: list[tuple[int, int | None, float]] = []
    for start, end in zip(cuts, cuts[1:], strict=False):
        if draw(st.booleans()):
            found.append((start, end, draw(st.sampled_from(VALUES))))
    if cuts and cuts[-1] < MINUTES and draw(st.booleans()):
        found.append((cuts[-1], None, draw(st.sampled_from(VALUES))))
    return found


def in_force(found: list[tuple[int, int | None, float]], base_value: float, j: int) -> float:
    """What a property holds at grid time j, read off the request."""
    now = j * 15
    for start, end, value in found:
        if start <= now and (end is None or now < end):
            return value
    return base_value


@settings(max_examples=40, deadline=None, database=None)
@given(asked=st.tuples(*(spans() for _ in PROPERTIES)), data=st.data())
def test_what_is_assumed_on_the_branch_is_what_the_request_says_is_in_force(
    world: World, asked: tuple[list[tuple[int, int | None, float]], ...], data: st.DataObject
):
    base: datetime = world.base.t
    interventions: list[dict[str, Any]] = [
        {
            "target": target,
            "uid": uid,
            "value": value,
            "start": base + timedelta(minutes=start),
            "end": None if end is None else base + timedelta(minutes=end),
        }
        for (target, _, _, uid, _), found in zip(PROPERTIES, asked, strict=True)
        for start, end, value in found
    ]
    # The order they are given in says nothing.
    interventions = data.draw(st.permutations(interventions))
    made = create(world, world.request(horizon=STEPS * STEP, interventions=interventions))
    uid, branch = made["uid"], made["branch"]
    flat = prepared({node("T1"): lambda j: 5.0, node("T2"): lambda j: 6.0}, STEPS, base)

    def predict(record: Any, **more: Any) -> pa.Table:
        return flat

    done = scenarios.run(world.twin, uid, predict=predict, budget_s=60.0)
    assert done["versions"] == STEPS

    for j in range(STEPS + 1):
        moment = base + j * STEP
        for (_, type_name, name, object_uid, base_value), found in zip(
            PROPERTIES, asked, strict=True
        ):
            # The last version of the branch at or before the grid time.
            value, provenance = cells(world, type_name, name, iso_utc(moment), branch)[object_uid]
            assert value == in_force(found, base_value, j), (j, object_uid, found)
            # Assumed from the first grid time an intervention on it has started by;
            # as it was observed until then.
            started = any(start <= j * 15 for start, _, _ in found)
            assert provenance["k"] == ("assumed" if started else "observed"), (j, object_uid)
            assert (provenance["s"] == uid) is started
    scenarios.delete(world.twin, uid)
