"""Derived rows, training sets, search, the health of what the twin computes with, and
what the API passes on when its worker is busy, fails or cannot be reached."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
from compute_testkit import ARROW, STEP, Small, World, at, level, plant_code, questions
from dta_runtime import training
from dta_runtime.protocol import FitRequest, FrameRef, TrainingSetRequest
from dta_runtime.worker.client import ApiTwin, Calls
from live_api_testkit import HEADERS, free_port, records

SCADA = {
    "connector": "file",
    "location": "/nowhere/scada",
    "refresh_period": "PT15M",
    "profile": {
        "profiled_at": "2026-01-01T00:00:00Z",
        "datasets": {
            "tanks": {
                "rows": 2,
                "columns": {
                    "tank": {"dtype": "string", "null_fraction": 0, "distinct_count": 2},
                    "level": {
                        "dtype": "float",
                        "null_fraction": 0,
                        "distinct_count": 2,
                        "noise_sigma": 0.05,
                    },
                },
            }
        },
    },
    "mappings": {
        "tanks": {
            "dataset": "tanks",
            "object_type": "Tank",
            "identity": ["tank"],
            "properties": {"level": {"column": "level"}},
        }
    },
}
SOURCE = "twin_function:tank_levels"


def with_source(make: Callable[..., Small], **sections: Any) -> Small:
    """A twin whose tank levels are filled from a source, with its registries in place."""
    api = make(history=3, sources={"scada": SCADA}, **sections)
    api.register(api.upload(plant_code()))
    return api


def derived(api: Small, **changes: Any) -> dict[str, Any]:
    body = {
        "branch": "main",
        "type": "Junction",
        "rows": [{"uid": "node:J1", "pressure": 3.5}, {"uid": "node:J2", "pressure": 4.5}],
        "kind": "inferred",
        "source": SOURCE,
        "inputs_version": api.head.id,
    }
    body.update(changes)
    return body


def test_derived_rows_are_written_as_one_version_with_their_lineage(
    small: Callable[..., Small],
) -> None:
    api = with_source(small)
    inputs = api.twin.store.resolve("#3")[0]
    before = api.head

    response = api.post("/derived", json=derived(api, inputs_version=inputs.id, confidence=0.6))

    assert response.status_code == 201, response.text
    head = api.head
    assert head.seq == before.seq + 1 and head.t == before.t
    assert response.json() == {
        "version": {"id": head.id, "seq": head.seq, "t": "2026-01-05T00:45:00Z", "branch": "main"},
        "rows": 2,
    }
    assert response.headers["x-dta-version-id"] == head.id
    written = api.twin.store.versions(head.id)[0]
    assert written.cause == "derived" and written.actor == SOURCE
    assert written.extra == {"source": SOURCE, "inputs_version": inputs.id, "kind": "inferred"}
    found = api.twin.store.get("node:J1")
    assert found.values["pressure"] == 3.5
    origin = found.provenance_of("pressure")
    assert origin.source == SOURCE and origin.kind == "inferred" and origin.confidence == 0.6
    # When no time is given, the rows were true at the time of what they were made from.
    assert origin.t_valid == inputs.t
    assert found.provenance_of("demand").kind == "observed"

    # On a branch, what is assumed may be written; the live line does not see it.
    api.twin.store.branch_create("what-if")
    assumed = api.post(
        "/derived",
        json=derived(
            api,
            branch="what-if",
            kind="assumed",
            rows=[{"uid": "node:J1", "pressure": 9.0}],
            t_valid="2026-01-05T00:30:00Z",
        ),  # fmt: skip
    )
    assert assumed.status_code == 201, assumed.text
    assert assumed.json()["version"]["branch"] == "what-if"
    there = api.twin.store.get("node:J1", branch="what-if")
    assert there.values["pressure"] == 9.0 and there.provenance_of("pressure").kind == "assumed"
    assert there.provenance_of("pressure").t_valid == at(2)
    assert api.twin.store.get("node:J1").values["pressure"] == 3.5


def test_derived_rows_that_may_not_be_written_are_refused_and_nothing_is_written(
    small: Callable[..., Small],
) -> None:
    api = with_source(small)
    before = api.head

    api.refused(
        api.post("/derived", json=derived(api, kind="observed")), 403, "derived.observed_refused"
    )
    for kind in ("assumed", "simulated"):
        api.refused(api.post("/derived", json=derived(api, kind=kind)), 409, "derived.live_refused")
    for source in ("twin_function:nothing", "node:J1", "twin_question:levels"):
        api.refused(
            api.post("/derived", json=derived(api, source=source)), 404, "derived.unknown_source"
        )
    api.refused(
        api.post("/derived", json=derived(api, inputs_version="nothing")),
        400,
        "derived.bad_inputs_version",
    )
    # A property a source fills is the sync's to write.
    mapped = api.refused(
        api.post(
            "/derived", json=derived(api, type="Tank", rows=[{"uid": "node:T1", "level": 1.0}])
        ),
        409,
        "derived.mapped_property",
    )
    assert "Tank.level" in mapped["detail"]
    many = [{"uid": f"node:X{number}", "pressure": 1.0} for number in range(100_001)]
    api.refused(api.post("/derived", json=derived(api, rows=many)), 413, "derived.too_large")
    huge = json.dumps(derived(api, rows=[{"uid": "node:J1", "notes": "x" * (17 * 1024 * 1024)}]))
    api.refused(api.post("/derived", content=huge.encode()), 413, "derived.too_large")
    api.refused(api.post("/derived", json=derived(api, type="Nothing")), 404, "store.unknown_type")
    api.refused(
        api.post("/derived", json=derived(api, branch="nothing")), 404, "store.unknown_branch"
    )
    for wrong in (
        derived(
            api, type="TwinQuestion", rows=[{"uid": "twin_question:levels", "status": "failing"}]
        ),
        derived(api, rows=[{"pressure": 1.0}]),
        derived(api, kind="guessed"),
        derived(api, confidence=2),
        {"branch": "main"},
    ):
        api.refused(api.post("/derived", json=wrong), 400, "api.bad_request")
    api.refused(api.post("/derived", content=b"not json"), 400, "api.bad_request")

    assert api.head.id == before.id


def test_a_question_answered_by_a_function_is_measured_by_latency_and_noise(
    small: Callable[..., Small],
) -> None:
    asked = questions()
    asked["tight"] = {**asked["levels"], "tolerance": {"metric": "mae", "value": 0.01, "unit": "m"}}
    asked["reports"] = {**asked["levels"], "function": "tank_report"}
    api = with_source(small, contract={"tier": "live", "questions": asked})

    result = api.done(api.post("/questions/measure", json={}))

    found = {one["id"]: one for one in result["questions"]}
    # What the tank levels are read from is noisier than nothing, and within the tolerance.
    assert found["levels"]["status"] == "validated" and found["levels"]["basis"] == "noise_floor"
    assert found["levels"]["measured_error"] == 0.05
    assert found["tight"]["status"] == "failing" and found["tight"]["basis"] == "noise_floor"
    assert "0.05" in found["tight"]["reason"] and "0.01" in found["tight"]["reason"]
    # Code of the twin's own cannot answer without a worker: the question fails.
    assert found["reports"]["status"] == "failing" and found["reports"]["basis"] == "latency"
    assert found["reports"]["reason"].startswith("worker.unavailable")
    # Three steps of history are too few to evaluate a model on.
    assert found["level_ahead"]["status"] == "provisional"
    assert "forecast origins" in found["level_ahead"]["reason"]
    failing = api.ok("/questions", status="failing")
    assert failing["version"]["id"] == result["version"]["id"]
    assert [one["id"] for one in failing["data"]] == ["reports", "tight"]
    health = api.ok("/meta/health")["compute"]
    assert health["worker"] == {"configured": False, "ok": None}
    assert health["questions"] == {"validated": 1, "provisional": 3, "failing": 2}
    answer = api.post("/questions/tight/answer").json()
    assert "question_failing" in answer["notes"] and answer["provisional"] is False


def test_a_training_set_is_what_the_training_package_gives_through_the_workers_reader(
    world: World, registered: dict[str, Any]
) -> None:
    ref = FrameRef(
        target="Tank.level",
        start=world.versions[4].id,
        end=world.versions[20].id,
        step=STEP,
    )
    expected = training.frame(world.twin.store, world.twin.ontology, ref)

    with httpx.Client(base_url=world.url, headers=HEADERS, timeout=60.0) as http:
        trace_id = uuid.uuid4().hex
        calls = Calls(http, deadline=time.monotonic() + 60.0, trace_id=trace_id)
        # The reader of a model run: pinned to where the run's frame ends.
        run = FitRequest(
            budget_s=60.0, trace_id=trace_id, family={"name": "theta"}, frame=ref, horizon=1
        )
        reader = ApiTwin(calls, run)
        assert (reader.at, reader.branch) == (world.versions[20].id, "main")
        read = reader.frame(ref)

    assert read.num_rows == 2 * 17 and read.column_names == ["unique_id", "ds", "y"]
    assert read.replace_schema_metadata(None).equals(expected.replace_schema_metadata(None))
    meta = json.loads(read.schema.metadata[b"dta:training"])
    assert meta["target"] == "Tank.level" and meta["branch"] == "main"

    body = {
        "target": "Tank.level",
        "uids": ["node:T1"],
        "start": "#5",
        "end": "#21",
        "step": "PT15M",
        "horizon": "PT30M",
    }
    labelled = training.training_set(
        world.twin.store,
        world.twin.ontology,
        TrainingSetRequest(**{**body, "start": world.versions[4].id, "end": world.versions[20].id}),
    )
    first = world.post("/block/training-set", json=body, params={"limit": 5})
    assert first.status_code == 200, first.text
    page = first.json()
    assert len(page["data"]) == 5 and page["next_cursor"]
    assert page["version"]["id"] == world.versions[20].id
    assert page["meta"]["target"] == "Tank.level" and page["meta"]["branch"] == "main"
    assert sorted(page["meta"]) == sorted(labelled.metadata)
    assert page["data"][0] == {
        "unique_id": "node:T1",
        "ds": at(4).isoformat().replace("+00:00", "Z"),
        "y": level("T1", 4),
        "label": level("T1", 6),
        "label_ds": at(6).isoformat().replace("+00:00", "Z"),
    }
    rest = world.post(
        "/block/training-set", json=body, params={"limit": 500, "cursor": page["next_cursor"]}
    ).json()
    assert len(page["data"]) + len(rest["data"]) == labelled.table.num_rows
    parquet = world.post("/block/training-set", json=body, params={"format": "parquet"})
    assert parquet.status_code == 200
    assert pa.BufferReader(parquet.content).size() > 0

    world.refused(
        world.post("/block/training-set", json={**body, "target": "Tank.name"}),
        400,
        "training.bad_spec",
    )
    world.refused(
        world.post("/block/training-set", json={**body, "step": "PT0.000001S"}),
        413,
        "training.too_large",
    )
    world.refused(
        world.post("/block/training-set", json={"target": "Tank.level"}), 400, "api.bad_request"
    )
    world.refused(
        world.post("/block/training-set", json={**body, "end": "#9999"}),
        404,
        "store.unknown_version",
    )


def test_objects_of_any_type_are_found_by_uid_or_name(
    world: World, registered: dict[str, Any]
) -> None:
    def found(**params: Any) -> list[tuple[str, str, str | None]]:
        return [
            (row["uid"], row["type"], row["name"]) for row in world.ok("/search", **params)["data"]
        ]

    assert found(q="%Tank%") == [("node:T1", "Tank", "Tank T1"), ("node:T2", "Tank", "Tank T2")]
    assert found(q="node:T_") == [("node:T1", "Tank", "Tank T1"), ("node:T2", "Tank", "Tank T2")]
    # An object that matches by its uid and by its name is given once.
    assert found(q="%J1%") == [("node:J1", "Junction", "Junction J1")]
    assert found(q="%1", types="Tank,Valve") == [
        ("link:V1", "Valve", None),
        ("node:T1", "Tank", "Tank T1"),
    ]
    assert found(q="nothing%") == []
    # The twin's own records are left out unless they are asked for.
    assert found(q="twin_function:tank%") == []
    assert [uid for uid, _, _ in found(q="twin_function:tank%", include_meta="true")] == [
        "twin_function:tank_levels",
        "twin_function:tank_report",
    ]
    assert [uid for uid, _, _ in found(q="%tank_levels", types="TwinFunction")] == [
        "twin_function:tank_levels"
    ]

    everything = world.ok("/search", q="%", count="true")
    total = everything["total"]
    assert total == len(everything["data"]) == 7
    first = world.ok("/search", q="%", limit=3, count="true")
    assert len(first["data"]) == 3 and first["total"] == total and first["next_cursor"]
    second = world.ok("/search", q="%", limit=3, cursor=first["next_cursor"])
    third = world.ok("/search", q="%", limit=3, cursor=second["next_cursor"])
    assert third["next_cursor"] is None
    assert first["data"] + second["data"] + third["data"] == everything["data"]
    assert world.ok("/search", q="node:%", at="#1")["data"] == []

    arrow = world.get("/search", q="node:%", format="arrow", count="true")
    assert arrow.headers["content-type"] == ARROW and arrow.headers["x-dta-total"] == "4"
    table = pa.ipc.open_stream(io.BytesIO(arrow.content)).read_all()
    assert table.column_names == ["uid", "type", "name"] and table.num_rows == 4
    world.refused(world.get("/search"), 400, "api.bad_request")
    world.refused(world.get("/search", q="%", types="Nothing"), 404, "store.unknown_type")


def test_a_twin_without_a_worker_says_so_and_one_that_cannot_be_reached_is_a_bad_gateway(
    small: Callable[..., Small], log_lines: Path
) -> None:
    alone = small(history=2)
    alone.register(alone.upload(plant_code()))

    alone.refused(
        alone.post("/functions/tank_report/run", json={"uid": "node:T1"}), 503, "worker.unavailable"
    )
    alone.refused(alone.post("/models/evaluate", json={}), 503, "worker.unavailable")
    # What needs no worker still answers.
    assert alone.post("/functions/tank_levels/run").status_code == 200
    assert alone.ok("/meta/health")["compute"]["worker"] == {"configured": False, "ok": None}

    lost = small(history=32, worker_url=f"http://127.0.0.1:{free_port()}")
    lost.register(lost.upload(plant_code()))
    refused = lost.refused(
        lost.post("/functions/tank_report/run", json={"uid": "node:T1"}), 502, "worker.unreachable"
    )
    assert "127.0.0.1" not in refused["detail"]
    assert lost.ok("/meta/health")["compute"]["worker"] == {"configured": True, "ok": False}
    failed = [
        found
        for found in records(log_lines)
        if found["context"] == "api.compute" and found["level"] == "error"
    ]
    assert len(failed) == 1 and failed[0]["data"]["op"] == "function"
    # A job that needs the worker fails with the same code, and records nothing: that
    # the worker is away says nothing of the candidates.
    before = lost.head
    job = lost.wait(lost.post("/models/evaluate", json={"spec": "level"}))
    assert job["state"] == "failed" and job["error"]["code"] == "worker.unreachable"
    assert lost.head.id == before.id and lost.store_rows("TwinBacktest") == {}


def test_a_busy_worker_is_passed_on_with_when_to_come_back(
    world: World, registered: dict[str, Any]
) -> None:
    pool = world.worker_app.state.pools.interactive
    asked = 2 * pool.size
    with ThreadPoolExecutor(max_workers=asked) as running:
        held = [running.submit(world.post, "/functions/sleepy/run", {"s": 4}) for _ in range(asked)]
        deadline = time.monotonic() + 30.0
        while pool.waiting < pool.size:
            assert time.monotonic() < deadline, "the worker's pool did not fill"
            time.sleep(0.02)

        refused = world.post("/functions/sleepy/run", json={"s": 0})

        world.refused(refused, 503, "worker.busy")
        assert 1 <= int(refused.headers["retry-after"]) <= 30
        answers = [found.result() for found in held]
    assert [found.status_code for found in answers] == [200] * asked
    assert all(found.json()["value"] == 4 for found in answers)
    assert world.post("/functions/sleepy/run", json={"s": 0}).status_code == 200


def test_the_api_process_loads_no_model_library() -> None:
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from dta_core.store import StoreSettings\n"
        "from dta_runtime.api.app import create_app\n"
        "from dta_runtime.api.docs import build_openapi\n"
        "from dta_runtime.settings import RuntimeSettings\n"
        "store = StoreSettings(host='127.0.0.1', port=1, user='u', password='p', database='d')\n"
        "settings = RuntimeSettings(store=store, api_token='t' * 32, "
        "artifacts_dir=Path('a'), logs_dir=Path('l'), worker_url='http://127.0.0.1:1')\n"
        "app = create_app(settings)\n"
        "assert '/api/v1/models/evaluate' in build_openapi(app)['paths']\n"
        "heavy = ('pandas', 'statsforecast', 'mlforecast', 'sklearn', 'scipy')\n"
        "print(','.join(name for name in heavy if name in sys.modules))\n"
    )

    ran = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False
    )

    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == ""
