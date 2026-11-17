"""Running a built-in: the deadline, the forecast it hands on, what it is refused for
whoever calls it, and what it logs."""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from dta_core.functions import BUILTINS
from dta_core.store import Selector
from dta_runtime.functions import Context, FunctionError, run
from dta_runtime.functions import reads as reads_module
from dta_runtime.graph import GraphCache
from dta_runtime.protocol import Answer
from functions_testkit import FUNCTION, History, node, rows

# One call of each built-in, with the parameters it cannot do without.
CALLS: dict[str, dict[str, Any]] = {
    "latest": {"property": "Tank.level"},
    "below": {"property": "Tank.level", "threshold": 2.0},
    "above": {
        "property": "Tank.level",
        "threshold": 2.0,
        "from": Selector("seq", seq=2),
        "to": Selector("head"),
    },
    "integrate": {"property": "Tank.level", "from": "#2", "to": "#9"},
    "paths": {"source": node("J1"), "target": node("J3")},
    "aggregate": {"property": "Pipe.length", "op": "sum"},
    "series": {"property": "Tank.level", "from": "#2", "to": "#9"},
    "forecast": {"question": "level_ahead"},
}


class Untouched:
    """Stands where the store does in a run that must not read: any use of it fails."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the store was used ({name}) after the deadline")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class Timed:
    """The store itself, noting each read and letting a second pass with it."""

    READS = ("resolve", "table", "changes", "get", "edges")

    def __init__(self, store: Any, clock: Clock) -> None:
        self._store = store
        self._clock = clock
        self.reads: list[str] = []

    def __getattr__(self, name: str) -> Any:
        found = getattr(self._store, name)
        if name not in self.READS:
            return found

        def read(*args: Any, **kwargs: Any) -> Any:
            self.reads.append(name)
            self._clock.now += 1.0
            return found(*args, **kwargs)

        return read


class Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def named(self, message: str) -> list[dict[str, Any]]:
        return [
            getattr(record, "data", {}) for record in self.records if record.getMessage() == message
        ]


@pytest.fixture
def logged() -> Iterator[Records]:
    logger = logging.getLogger("dta")
    handler = Records()
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


def test_every_built_in_of_the_table_can_be_run(story: History):
    assert set(CALLS) == set(BUILTINS)
    context = story.context(forecast=lambda params, context: Answer(value=1.0))
    for entry, call in CALLS.items():
        assert isinstance(run(FUNCTION, entry, call, context), Answer), entry


@pytest.mark.parametrize("entry", sorted(CALLS))
def test_a_run_whose_deadline_has_passed_gives_up_without_reading(story: History, entry):
    untouched = Untouched()
    context = dataclasses.replace(
        story.context(forecast=lambda params, context: Answer(value=1.0)),
        store=untouched,
        graphs=GraphCache(untouched),  # type: ignore[arg-type]
        deadline=time.monotonic() - 0.001,
    )
    with pytest.raises(FunctionError) as raised:
        run(FUNCTION, entry, CALLS[entry], context)
    assert raised.value.code == "function.timeout"
    assert raised.value.detail == {"function": FUNCTION, "entry": entry, "reads": 0}


def test_no_read_is_started_after_the_deadline(story: History, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(reads_module, "time", clock)
    timed = Timed(story.twin.store, clock)
    context = dataclasses.replace(story.context(), store=timed, deadline=1.5)
    call = {"property": "Tank.level", "against": "#3"}
    # The version is looked up, the tanks are read, and by then the time is up: the
    # read of the tanks as they were is never made.
    with pytest.raises(FunctionError) as raised:
        run(FUNCTION, "latest", call, context)
    assert raised.value.code == "function.timeout"
    assert raised.value.detail["reads"] == 2
    assert timed.reads == ["resolve", "table"]
    # With time for all three, the answer.
    clock.now = 0.0
    enough = dataclasses.replace(context, deadline=3.5)
    assert len(rows(run(FUNCTION, "latest", call, enough))) == 4
    assert timed.reads == ["resolve", "table", "resolve", "table", "table"]


def test_a_range_is_two_reads_of_the_store_whatever_its_length(story: History):
    clock = Clock()
    timed = Timed(story.twin.store, clock)
    context = dataclasses.replace(story.context(), store=timed)
    run(FUNCTION, "series", {"property": "Tank.level", "from": "#2", "to": "head"}, context)
    # `head` is the pinned version and is not looked up.
    assert timed.reads == ["resolve", "table", "changes"]
    timed.reads.clear()
    run(
        FUNCTION,
        "integrate",
        {"property": "Tank.level", "from": "#2", "to": "#9", "period": timedelta(hours=1)},
        context,
    )
    assert timed.reads == ["resolve", "resolve", "table", "changes"]
    timed.reads.clear()
    # An abstract class: the two reads once per concrete type under it.
    run(FUNCTION, "series", {"property": "Node.elevation", "from": "#2", "to": "head"}, context)
    assert timed.reads == ["resolve", *["table", "changes"] * 3]


def test_a_forecast_is_handed_to_whoever_makes_them(story: History):
    seen: list[tuple[Any, Context]] = []

    def forecast(params: Any, context: Context) -> Answer:
        seen.append((dict(params), context))
        return Answer(value=3.2, unit="m", notes=("outside_validity",))

    context = story.context(forecast=forecast)
    call = {"question": "level_ahead", "horizon": timedelta(hours=6), "uids": [node("T1")]}
    answer = run(FUNCTION, "forecast", call, context)
    assert answer == Answer(value=3.2, unit="m", notes=("outside_validity",))
    # What the table gives a parameter that was left out comes with the rest.
    assert seen == [({**call, "levels": (80, 95)}, context)]


def test_a_forecast_with_nothing_to_make_it_is_unavailable(story: History):
    with pytest.raises(FunctionError) as raised:
        run(FUNCTION, "forecast", {"question": "level_ahead"}, story.context())
    assert raised.value.code == "function.unavailable"
    assert raised.value.detail == {"function": FUNCTION, "entry": "forecast"}


def test_parameters_are_checked_against_the_table_whoever_calls(story: History):
    context = story.context()
    cases = [
        ("median", {"property": "Tank.level"}, "function.unknown_entry", None),
        (
            "latest",
            {"property": "Tank.level", "threshold": 2.0},
            "function.param_unknown",
            "threshold",
        ),
        ("below", {"property": "Tank.level"}, "function.param_missing", "threshold"),
        (
            "latest",
            {"property": "Tank.level", "against": "#1..#3"},
            "function.bad_param",
            "against",
        ),
    ]
    for entry, params, code, parameter in cases:
        with pytest.raises(FunctionError) as raised:
            run(FUNCTION, entry, params, context)
        assert raised.value.code == code
        assert raised.value.detail["function"] == FUNCTION
        assert raised.value.detail.get("parameter") == parameter


def test_a_run_is_logged_with_what_it_cost(story: History, logged: Records):
    run(FUNCTION, "latest", {"property": "Tank.level"}, story.context())
    (done,) = logged.named("function run")
    assert done["function"] == FUNCTION and done["entry"] == "latest"
    assert done["version"] == story.head.id and done["seq"] == story.head.seq
    assert done["rows"] == 4 and done["reads"] == 1
    assert done["duration_ms"] >= 0


def test_a_refusal_is_logged_with_its_code(story: History, logged: Records):
    with pytest.raises(FunctionError):
        run(FUNCTION, "below", {"property": "Sensor.quantity", "threshold": 2.0}, story.context())
    (refused,) = logged.named("function refused")
    assert refused["code"] == "function.bad_param"
    assert refused["detail"]["parameter"] == "property"
    assert refused["function"] == FUNCTION and refused["entry"] == "below"
    assert not logged.named("function run")


def test_an_error_of_the_store_passes_through_and_is_logged(story: History, logged: Records):
    from dta_core.store import UnknownType

    with pytest.raises(UnknownType) as raised:
        run(FUNCTION, "latest", {"property": "Cistern.level"}, story.context())
    assert raised.value.code == "store.unknown_type"
    (refused,) = logged.named("function refused")
    assert refused["code"] == "store.unknown_type"
