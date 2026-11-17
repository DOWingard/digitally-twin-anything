"""Running the functions of the twin through the API: the built-ins in the API itself,
the twin's own code in the real worker, and what a run that gives rows writes."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from api_testkit import TOKEN
from compute_testkit import ARROW, LAST, STEP, World, at, demand, level
from dta_core.manifest.models import FunctionSpec
from dta_runtime import functions
from dta_runtime.api.formats import dumps, rows
from dta_runtime.graph import GraphCache
from live_api_testkit import records

BUILTIN_CALLS = [
    ("latest", {"property": "Tank.level"}),
    ("latest", {"property": "Junction.demand", "against": "#5", "uids": ["node:J1"]}),
    ("below", {"property": "Tank.level", "threshold": 5}),
    ("above", {"property": "Junction.demand", "threshold": 15, "from": "#4", "to": "#30"}),
    ("integrate", {"property": "Tank.level", "from": "#4", "to": "#30", "period": "PT1H"}),
    ("paths", {"source": "node:J1", "target": "node:T1"}),
    ("aggregate", {"property": "Tank.level", "op": "mean"}),
    ("aggregate", {"property": "Junction.demand", "op": "max"}),
    ("series", {"property": "Tank.level", "from": "#10", "to": "#20", "uids": ["node:T2"]}),
]


def plain(value: Any) -> Any:
    """A value as the API's JSON gives it."""
    return json.loads(dumps({"value": rows(value) if isinstance(value, pa.Table) else value}))[
        "value"
    ]


def runtime_function(world: World, name: str, entry: str, **more: Any) -> None:
    """Register a function while the twin runs, unless an earlier test did."""
    response = world.post("/functions", json={"id": name, "entry": entry, **more})
    assert response.status_code in (201, 409), response.text


@pytest.mark.parametrize(("entry", "call"), BUILTIN_CALLS)
def test_a_built_in_answers_through_the_route_what_the_function_package_gives(
    world: World, registered: dict[str, Any], entry: str, call: dict[str, Any]
) -> None:
    name = f"plain_{entry}"
    runtime_function(world, name, entry)
    version = world.head

    response = world.post(f"/functions/{name}/run", json=call, params={"at": version.id})

    assert response.status_code == 200, response.text
    body = response.json()
    spec = FunctionSpec(kind="builtin", entry=entry)
    context = functions.Context(
        store=world.twin.store,
        ontology=world.twin.store.ontology_at(version),
        version=version,
        graphs=GraphCache(world.twin.store, capacity=1),
        deadline=time.monotonic() + 60.0,
    )
    expected = functions.run(name, entry, functions.resolve(name, spec, call), context)
    if isinstance(expected.value, pa.Table):
        assert expected.value.num_rows > 0
        assert body["next_cursor"] is None
    assert body["value"] == plain(expected.value)
    assert body["unit"] == expected.unit
    assert body["notes"] == list(expected.notes)
    assert body["function"] == name and body["model"] is None
    assert body["provisional"] is False and body["reasons"] == []
    assert body["version"]["id"] == version.id
    assert response.headers["x-dta-version-id"] == version.id
    # A run that gives no rows to write changes nothing.
    assert world.head.id == version.id


def test_a_built_in_takes_its_arguments_from_the_body_or_the_query(
    world: World, registered: dict[str, Any]
) -> None:
    whole = world.post("/functions/tank_levels/run")
    assert whole.status_code == 200, whole.text
    found = {row["uid"]: row["value"] for row in whole.json()["value"]}
    assert found == {"node:T1": level("T1", LAST), "node:T2": level("T2", LAST)}
    assert whole.json()["unit"] == "m"

    by_query = world.post("/functions/tank_levels/run", params={"uids": "node:T2"})
    assert [row["uid"] for row in by_query.json()["value"]] == ["node:T2"]
    by_body = world.post("/functions/tank_levels/run", json={"uids": ["node:T2"]})
    assert by_body.json()["value"] == by_query.json()["value"]
    earlier = world.post("/functions/tank_levels/run", params={"at": world.versions[10].id})
    assert {row["uid"]: row["value"] for row in earlier.json()["value"]} == {
        "node:T1": level("T1", 10),
        "node:T2": level("T2", 10),
    }

    world.refused(
        world.post("/functions/tank_levels/run", json={"uids": ["node:T1"]}, params={"uids": "x"}),
        400,
        "api.bad_request",
    )
    world.refused(world.post("/functions/tank_levels/run", content=b"[1]"), 400, "api.bad_request")
    world.refused(
        world.post("/functions/tank_levels/run", json={"property": "Tank.elevation"}),
        400,
        "function.param_fixed",
    )
    world.refused(
        world.post("/functions/tank_levels/run", json={"nothing": 1}), 400, "function.param_unknown"
    )
    runtime_function(world, "plain_below", "below")
    world.refused(
        world.post("/functions/plain_below/run", json={"property": "Tank.level"}),
        400,
        "function.param_missing",
    )
    world.refused(world.post("/functions/nothing/run"), 404, "function.not_found")
    # The function is the one registered now, also for data older than its registration.
    assert world.post("/functions/tank_levels/run", params={"at": "#1"}).json()["value"] == []
    world.refused(
        world.post("/functions/tank_levels/run", params={"at": "#9999"}),
        404,
        "store.unknown_version",
    )


def test_a_table_answer_is_paged_as_json_and_whole_as_arrow(
    world: World, registered: dict[str, Any]
) -> None:
    version = world.head
    call = {"property": "Tank.level", "from": "#10", "to": "#20"}
    runtime_function(world, "plain_series", "series")
    whole = world.post("/functions/plain_series/run", json=call, params={"at": version.id})
    expected = whole.json()["value"]
    assert len(expected) > 4

    first = world.post(
        "/functions/plain_series/run", json=call, params={"at": version.id, "limit": 4}
    ).json()
    assert first["value"] == expected[:4] and first["next_cursor"]
    second = world.post(
        "/functions/plain_series/run",
        json=call,
        params={"cursor": first["next_cursor"], "limit": 4},
    ).json()
    assert second["value"] == expected[4:8]
    assert second["version"]["id"] == version.id
    # A cursor belongs to the call it was given for.
    world.refused(
        world.post(
            "/functions/plain_series/run",
            json={**call, "to": "#21"},
            params={"cursor": first["next_cursor"]},
        ),
        400,
        "api.bad_cursor",
    )

    arrow = world.post(
        "/functions/plain_series/run", json=call, params={"at": version.id, "format": "arrow"}
    )
    assert arrow.status_code == 200 and arrow.headers["content-type"] == ARROW
    table = pa.ipc.open_stream(io.BytesIO(arrow.content)).read_all()
    assert plain(table) == expected
    document = json.loads(table.schema.metadata[b"dta:answer"])
    assert document["function"] == "plain_series" and document["version"]["id"] == version.id
    assert document["provisional"] is False and "value" not in document


def test_a_built_in_gives_up_at_its_latency_budget(
    world: World, registered: dict[str, Any]
) -> None:
    runtime_function(
        world,
        "hurried",
        "series",
        params={"property": "Tank.level", "from": "#2", "to": "head"},
        latency_budget="PT0.001S",
    )

    refused = world.refused(world.post("/functions/hurried/run"), 504, "function.timeout")

    # Either the function saw its deadline between two reads, or the route stopped waiting
    # for it: which comes first is a matter of a millisecond.
    assert refused["detail"].startswith("hurried: ")


def test_code_of_the_twin_runs_in_the_worker_and_writes_nothing(
    world: World, registered: dict[str, Any], log_lines: Path
) -> None:
    before = world.head
    version = world.versions[10]

    response = world.post(
        "/functions/tank_report/run", json={"uid": "node:T1"}, params={"at": version.id}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["value"] == {"level": level("T1", 10), "at": version.id, "branch": "main"}
    assert body["function"] == "tank_report" and body["version"]["id"] == version.id
    assert "wrote" not in body or body["wrote"] is None
    assert world.head.id == before.id
    assert world.store_rows("TwinFunctionRun") == {}
    logged = [
        found
        for found in records(log_lines)
        if found["message"] == "function run" and found["data"]["function"] == "tank_report"
    ]
    assert len(logged) == 1 and logged[0]["level"] == "info"
    assert logged[0]["data"]["wrote"] is None and logged[0]["data"]["duration_ms"] > 0
    calls = [found for found in records(log_lines) if found["context"] == "api.compute"]
    assert len(calls) == 1 and calls[0]["level"] == "info"
    call = calls[0]["data"]
    assert call["op"] == "function" and call["subject"] == "compute_plant:report"
    assert call["outcome"] == "ok" and call["duration_ms"] > 0
    # Neither the arguments of the call nor the token are in what is logged of it.
    assert "node:T1" not in json.dumps(calls) and TOKEN not in log_lines.read_text()

    world.refused(
        world.post("/functions/tank_report/run", params={"uid": "node:T1"}),
        400,
        "api.bad_request",
    )
    world.refused(world.post("/functions/tank_report/run", json={}), 400, "function.bad_param")


def test_code_that_raises_is_passed_on_with_the_frames_of_its_own_code(
    world: World, registered: dict[str, Any]
) -> None:
    refused = world.refused(world.post("/functions/stuck/run"), 500, "function.failed")

    assert "valve stuck" in refused["detail"]
    assert refused["frames"]
    assert any("broken" in json.dumps(frame) for frame in refused["frames"])


def test_rows_a_function_gives_are_written_as_one_version_with_the_record_of_its_run(
    world: World, registered: dict[str, Any]
) -> None:
    before = world.head

    response = world.post("/functions/junction_pressures/run")

    assert response.status_code == 200, response.text
    body = response.json()
    head = world.head
    assert head.seq == before.seq + 1 and head.t == before.t
    assert body["wrote"]["id"] == head.id and body["rows_written"] == 2
    assert body["version"]["id"] == before.id and body["value"] is None
    written = world.twin.store.versions(head.id)[0]
    assert written.cause == "derived" and written.actor == "twin_function:junction_pressures"
    assert written.extra == {
        "source": "twin_function:junction_pressures",
        "inputs_version": before.id,
        "kind": "inferred",
    }
    for key in ("J1", "J2"):
        found = world.twin.store.get(f"node:{key}")
        assert found.values["pressure"] == 2.0 * demand(key, LAST)
        origin = found.provenance_of("pressure")
        assert origin.source == "twin_function:junction_pressures"
        assert origin.kind == "inferred" and origin.confidence == 0.75
        assert origin.t_valid == before.t
        # What the sync wrote is still the sync's.
        assert found.provenance_of("demand").source == "scada"
        assert found.provenance_of("demand").kind == "observed"
    runs = list(world.store_rows("TwinFunctionRun").values())
    assert len(runs) == 1
    run = runs[0]
    assert run["function"] == "twin_function:junction_pressures"
    assert run["status"] == "succeeded" and run["trigger"] == "on_request"
    assert run["rows_written"] == 2 and run["at_version"] == before.id
    assert json.loads(run["params"]) == {"factor": 2.0}
    assert world.twin.store.get(run["uid"]).provenance_of("status").source == "registry"


def test_a_forecast_is_what_the_active_model_of_its_question_predicts(
    world: World, evaluated: dict[str, Any]
) -> None:
    version = world.head

    response = world.post("/functions/level_forecast/run")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == "twin_model:level/naive" and body["unit"] == "m"
    assert body["version"]["id"] == version.id
    assert body["provisional"] is False and body["reasons"] == []
    predicted = {(row["unique_id"], row["ds"]): row for row in body["value"]}
    assert len(predicted) == 8
    for key in ("T1", "T2"):
        for ahead in range(1, 5):
            moment = at(LAST + ahead).isoformat().replace("+00:00", "Z")
            row = predicted[(f"node:{key}", moment)]
            assert row["mean"] == pytest.approx(level(key, LAST + ahead))
            assert row["lo_95"] <= row["lo_80"] <= row["mean"] <= row["hi_80"] <= row["hi_95"]
    assert world.head.id == version.id

    shorter = world.post(
        "/functions/level_forecast/run",
        params={"horizon": "PT30M", "uids": "node:T1", "levels": "50"},
    ).json()
    assert [row["unique_id"] for row in shorter["value"]] == ["node:T1"] * 2
    assert "lo_50" in shorter["value"][0] and "lo_80" not in shorter["value"][0]
    earlier = world.post(
        "/functions/level_forecast/run", params={"at": world.versions[40].id, "uids": "node:T1"}
    ).json()
    assert earlier["value"][0]["mean"] == pytest.approx(level("T1", 41))
    assert earlier["value"][0]["ds"] == (at(40) + STEP).isoformat().replace("+00:00", "Z")

    world.refused(world.post("/functions/demand_forecast/run"), 409, "model.none_active")
    runtime_function(world, "lost_forecast", "forecast", params={"question": "nothing"})
    world.refused(world.post("/functions/lost_forecast/run"), 400, "function.bad_param")
