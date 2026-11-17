"""Evaluating the candidates of the twin's models, predicting with one, and measuring
what the questions may claim: all through the API, with the real worker."""

from __future__ import annotations

import io
import json
from datetime import datetime
from typing import Any

import pyarrow as pa
import pytest
from compute_testkit import ARROW, INFLOW, LAST, OUTFLOW, World, at, level
from live_api_testkit import wait_for_job

NAIVE = "twin_model:level/naive"
PLANT = "twin_model:whatif/plant"


def iso(steps: float) -> str:
    return at(steps).isoformat().replace("+00:00", "Z")


def of_spec(result: dict[str, Any], name: str) -> dict[str, Any]:
    return next(found for found in result["specs"] if found["spec"] == name)


def test_evaluating_backtests_every_candidate_and_activates_the_best_in_one_version(
    world: World, evaluated: dict[str, Any]
) -> None:
    assert [found["spec"] for found in evaluated["specs"]] == ["demand", "level", "whatif"]
    found = of_spec(evaluated, "level")
    assert found["selected"] == NAIVE and found["question"] == "level_ahead"
    assert found["plan"]["horizon"] == 4 and found["plan"]["enough"] is True
    assert found["plan"]["windows"] >= 3
    candidates = {one["candidate"]: one for one in found["candidates"]}
    naive = candidates["naive"]
    assert naive["status"] == "active" and naive["accepted"] is True
    assert naive["error"] == pytest.approx(0.0, abs=1e-9) and naive["reasons"] == []
    assert naive["baseline_scores"]["mae"] > 0.1 and naive["folds"] == found["plan"]["windows"]
    for name in ("theta", "lags"):
        other = candidates[name]
        assert other["evaluated"] is True and other["note"] == "evaluated"
        assert other["status"] == ("candidate" if other["accepted"] else "rejected")
        assert other["error"] >= 0 and other["backtest"].startswith(f"twin_backtest:level/{name}@")
    # The candidate whose settings its family refuses failed, and the others went on.
    broken = candidates["broken"]
    assert broken["note"] == "failed" and broken["failed"]["code"] == "model.bad_params"
    assert broken["status"] == "candidate" and "accepted" not in broken
    assert found["fitted"]["series"] == 2 and found["fitted"]["fitted_to"] == iso(LAST)
    assert found["question_status"] == "validated"

    # Everything the job found is one version, written at the time of the head it read.
    assert evaluated["version"]["seq"] == evaluated["at"]["seq"] + 1
    assert evaluated["version"]["t"] == evaluated["at"]["t"]
    written = world.twin.store.versions(evaluated["version"]["id"])[0]
    assert written.cause == "registration" and written.actor == "registry"
    assert written.extra["evaluated"] == ["demand", "level", "whatif"]

    models = world.store_rows("TwinModel")
    active = models[NAIVE]
    assert active["status"] == "active" and active["fitted"].startswith("twin_artifact:")
    assert active["fitted_at_version"] == evaluated["at"]["id"]
    assert active["fitted_to"] == at(LAST) and active["fitted_from"] == at(1)
    assert models["twin_model:level/broken"]["status"] == "candidate"
    assert models["twin_model:level/broken"]["fitted"] is None
    assert [uid for uid, row in models.items() if row["status"] == "active"] == [NAIVE, PLANT]

    backtests: dict[str, list[dict[str, Any]]] = {}
    for row in world.store_rows("TwinBacktest").values():
        backtests.setdefault(row["model"], []).append(row)
    assert sorted(backtests) == [
        "twin_model:level/broken",
        "twin_model:level/lags",
        NAIVE,
        "twin_model:level/theta",
        PLANT,
    ]
    assert all(len(rows) == 1 for rows in backtests.values())
    kept = backtests[NAIVE][0]
    assert kept["accepted"] is True and kept["error"] == pytest.approx(0.0, abs=1e-9)
    assert kept["metric"] == "mae" and kept["threshold"] == 0.5
    assert kept["baseline"] == "persistence" and kept["baseline_error"] > 0.1
    assert kept["origins"] == found["plan"]["windows"] and kept["horizon_steps"] == 4
    assert kept["step"] == "PT15M" and kept["at_version"] == evaluated["at"]["id"]
    assert kept["range_from"] == at(1) and kept["range_to"] == at(LAST)
    assert kept["residuals"].startswith("twin_artifact:")
    assert json.loads(kept["scores"])["model"]["mae"] == pytest.approx(0.0, abs=1e-9)
    failed = backtests["twin_model:level/broken"][0]
    assert failed["origins"] == 0 and failed["accepted"] is False and failed["error"] is None
    assert json.loads(failed["scores"])["error"]["code"] == "model.bad_params"
    artifacts = world.store_rows("TwinArtifact")
    assert active["fitted"] in artifacts and kept["residuals"] in artifacts
    assert artifacts[active["fitted"]]["media_type"] == "application/vnd.dta.model-state"

    question = world.store_rows("TwinQuestion")["twin_question:level_ahead"]
    assert question["status"] == "validated" and question["basis"] == "backtest"
    assert question["measured_error"] == pytest.approx(0.0, abs=1e-9)
    assert question["model"] == NAIVE and question["backtest"] == kept["uid"]
    assert question["reason"] is None and question["measured_at"] == at(LAST)

    # The twin's own family was walked through the history by its own code.
    own = of_spec(evaluated, "whatif")
    assert own["selected"] == PLANT and own["question_status"] == "validated"
    assert models[PLANT]["status"] == "active" and models[PLANT]["fitted"] is not None


def test_a_history_too_short_for_the_folds_leaves_the_question_provisional(
    world: World, evaluated: dict[str, Any]
) -> None:
    found = of_spec(evaluated, "demand")

    assert found["selected"] is None and found["plan"]["enough"] is False
    assert "forecast origins" in found["skipped"]
    assert found["candidates"][0]["note"] == "not_evaluated"
    assert found["question_status"] == "provisional"
    question = world.ok("/questions/demand_ahead")["data"]
    assert question["status"] == "provisional" and question["reason"] == found["skipped"]
    assert question["basis"] is None and question["measured_error"] is None
    assert world.ok("/models/twin_model:demand/last")["data"]["status"] == "candidate"
    assert world.ok("/models/twin_model:demand/last/backtests")["data"] == []


def test_what_was_evaluated_is_read_through_the_api(
    world: World, evaluated: dict[str, Any]
) -> None:
    one = world.ok(f"/models/{NAIVE}")["data"]
    assert one["status"] == "active" and len(one["fitted"]) == 64
    state = one["state"]
    assert state["family"] == {"name": "seasonal_naive"}
    assert datetime.fromisoformat(state["fitted_from"]) == at(1)
    assert datetime.fromisoformat(state["fitted_to"]) == at(LAST)
    assert state["series"] == 2 and "python" in state["versions"]
    assert one["validity"] == state["validity"]
    assert [found["uid"] for found in world.ok("/models", status="active")["data"]] == [
        NAIVE,
        PLANT,
    ]

    backtests = world.ok(f"/models/{NAIVE}/backtests")["data"]
    assert len(backtests) >= 1
    newest = backtests[0]
    assert newest["model"] == NAIVE and newest["accepted"] is True
    assert newest["scores"]["model"]["mae"] == pytest.approx(0.0, abs=1e-9)
    assert newest["scores"]["baseline"]["mae"] > 0.1 and len(newest["residuals"]) == 64
    failed = world.ok("/models/twin_model:level/broken/backtests")["data"][0]
    assert failed["origins"] == 0 and failed["scores"]["error"]["code"] == "model.bad_params"

    validated = world.ok("/questions", status="validated")["data"]
    assert {"level_ahead", "level_whatif"} <= {found["id"] for found in validated}
    health = world.ok("/meta/health")["compute"]
    assert health["worker"] == {"configured": True, "ok": True}
    assert sum(health["questions"].values()) == 4 and health["questions"]["validated"] >= 2
    assert health["job"] is None


def test_evaluating_is_started_once_for_a_key_and_stops_at_its_budget(
    world: World, evaluated: dict[str, Any]
) -> None:
    jobs = world.ok("/lifecycle/jobs", kind="evaluate")["data"]
    first = next(job for job in jobs if job["idempotency_key"] == "evaluate-all")
    assert first["lane"] == "compute" and first["state"] == "succeeded"

    again = world.post("/models/evaluate", json={}, headers={"Idempotency-Key": "evaluate-all"})

    assert again.status_code == 200 and again.json()["job"]["id"] == first["id"]
    assert again.json()["job"]["result"] == evaluated
    world.refused(
        world.post(
            "/models/evaluate", json={"spec": "level"}, headers={"Idempotency-Key": "evaluate-all"}
        ),
        409,
        "job.key_reused",
    )
    world.refused(
        world.post("/models/evaluate", json={"spec": "nothing"}), 404, "model.unknown_spec"
    )
    world.refused(world.post("/models/evaluate", json={"budget_s": 0}), 400, "api.bad_request")
    assert len(world.ok("/lifecycle/jobs", kind="evaluate")["data"]) == len(jobs)

    # A job out of budget starts no backtest: it says what it left, and changes no model.
    before = world.store_rows("TwinBacktest")
    result = world.done(world.post("/models/evaluate", json={"spec": "level", "budget_s": 1e-6}))
    found = of_spec(result, "level")
    assert {one["note"] for one in found["candidates"]} == {"not_evaluated"}
    assert found["selected"] is None
    assert world.store_rows("TwinBacktest") == before
    assert world.ok(f"/models/{NAIVE}")["data"]["status"] == "active"
    assert world.ok("/questions/level_ahead")["data"]["status"] == "validated"


def test_a_compute_job_is_listed_in_its_lane_while_a_sync_run_can_still_start(
    world: World, evaluated: dict[str, Any]
) -> None:
    started = world.post("/models/evaluate", json={"spec": "level"})
    assert started.status_code == 202, started.text
    job_id = started.json()["job"]["id"]
    assert started.json()["job"]["lane"] == "compute"
    # Asked again while it is queued or running, the same job is given back.
    same = world.post("/models/evaluate", json={"spec": "level"})
    assert same.status_code == 200 and same.json()["job"]["id"] == job_id

    listed = world.ok("/lifecycle/jobs", lane="compute", kind="evaluate")["data"]
    assert listed[0]["id"] == job_id and listed[0]["state"] in ("queued", "running")
    health = world.ok("/meta/health")["compute"]["job"]
    assert health["id"] == job_id and health["kind"] == "evaluate"
    assert health["state"] in ("queued", "running")

    sync = world.post("/sync/runs")
    assert sync.status_code == 202, sync.text
    assert sync.json()["job"]["lane"] == "sync"
    sync_id = sync.json()["job"]["id"]
    ended = wait_for_job(lambda: world.ok(f"/lifecycle/jobs/{sync_id}")["data"])
    # The sync run has ended in its own lane while the evaluation is still at work.
    assert ended["state"] in ("succeeded", "failed")
    assert world.ok(f"/lifecycle/jobs/{job_id}")["data"]["state"] in ("queued", "running")

    result = world.done(started)
    assert of_spec(result, "level")["selected"] == NAIVE
    assert world.ok("/meta/health")["compute"]["job"] is None
    assert len(world.ok(f"/models/{NAIVE}/backtests")["data"]) >= 2


def test_a_model_predicts_from_a_version_with_the_intervals_asked_for(
    world: World, evaluated: dict[str, Any]
) -> None:
    head = world.head

    response = world.post(f"/models/{NAIVE}/predict", json={"horizon": 4, "levels": [80]})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == NAIVE and body["version"]["id"] == head.id
    assert isinstance(body["notes"], list) and "no_intervals" not in body["notes"]
    rows = {(row["unique_id"], row["ds"]): row for row in body["data"]}
    assert len(rows) == 8
    for key in ("T1", "T2"):
        for ahead in range(1, 5):
            row = rows[(f"node:{key}", iso(LAST + ahead))]
            assert row["mean"] == pytest.approx(level(key, LAST + ahead))
            assert row["lo_80"] <= row["mean"] <= row["hi_80"]
            assert set(row) == {"unique_id", "ds", "mean", "lo_80", "hi_80"}
    assert world.head.id == head.id

    by_duration = world.post(f"/models/{NAIVE}/predict", json={"horizon": "PT30M"}).json()
    assert len(by_duration["data"]) == 4 and set(by_duration["data"][0]) == {
        "unique_id",
        "ds",
        "mean",
    }
    # Without a horizon, the one its question states.
    assert len(world.post(f"/models/{NAIVE}/predict").json()["data"]) == 8
    earlier = world.post(
        f"/models/{NAIVE}/predict", json={"horizon": 1}, params={"at": world.versions[30].id}
    ).json()
    assert {row["ds"] for row in earlier["data"]} == {iso(31)}
    assert earlier["data"][0]["mean"] == pytest.approx(level("T1", 31))
    paged = world.post(f"/models/{NAIVE}/predict", json={"horizon": 4}, params={"limit": 3}).json()
    assert len(paged["data"]) == 3 and paged["next_cursor"]

    arrow = world.post(
        f"/models/{NAIVE}/predict",
        json={"horizon": 2, "levels": [80, 95]},
        params={"format": "arrow"},
    )
    assert arrow.status_code == 200 and arrow.headers["content-type"] == ARROW
    table = pa.ipc.open_stream(io.BytesIO(arrow.content)).read_all()
    assert table.num_rows == 4
    assert table.column_names == ["unique_id", "ds", "mean", "lo_80", "hi_80", "lo_95", "hi_95"]
    assert json.loads(table.schema.metadata[b"dta:prediction"])["model"] == NAIVE


def test_a_prediction_is_refused_for_what_a_model_cannot_do(
    world: World, evaluated: dict[str, Any]
) -> None:
    closed = {"target": "Valve.status", "uid": "link:V1", "value": "closed", "start": iso(LAST + 2)}

    world.refused(
        world.post("/models/twin_model:level/broken/predict", json={"horizon": 2}),
        409,
        "model.not_fitted",
    )
    world.refused(
        world.post(f"/models/{NAIVE}/predict", json={"horizon": 2, "interventions": [closed]}),
        409,
        "model.not_interventional",
    )
    world.refused(
        world.post("/models/twin_model:level/nothing/predict", json={"horizon": 2}),
        404,
        "model.not_found",
    )
    for body in (
        {"horizon": 0},
        {"horizon": "-PT1H"},
        {"horizon": 2, "levels": [100]},
        {"horizon": 2, "nothing": 1},
    ):
        world.refused(world.post(f"/models/{NAIVE}/predict", json=body), 400, "api.bad_request")
    world.refused(
        world.post(f"/models/{PLANT}/predict", json={"horizon": 2, "interventions": [{"uid": 1}]}),
        400,
        "api.bad_request",
    )

    # The twin's own family holds under an intervention: closing V1 stops what fills T1.
    body = world.post(
        f"/models/{PLANT}/predict", json={"horizon": 4, "interventions": [closed]}
    ).json()
    means = {(row["unique_id"], row["ds"]): row["mean"] for row in body["data"]}
    gain, loss = INFLOW - OUTFLOW, -OUTFLOW
    assert means[("node:T1", iso(LAST + 1))] == pytest.approx(level("T1", LAST) + gain)
    assert means[("node:T1", iso(LAST + 4))] == pytest.approx(level("T1", LAST) + gain + 3 * loss)
    assert means[("node:T2", iso(LAST + 4))] == pytest.approx(level("T2", LAST) + 4 * gain)


def test_measuring_records_what_each_question_may_claim(
    world: World, evaluated: dict[str, Any]
) -> None:
    # Not measured yet, the question's answers say so.
    before = world.post("/questions/levels/answer")
    assert before.status_code == 200, before.text
    assert before.json()["provisional"] is True
    assert before.json()["reasons"] == ["not measured yet"]
    assert before.json()["function"] == "tank_levels"
    head = world.head

    started = world.post("/questions/measure", json={}, headers={"Idempotency-Key": "measure-1"})
    assert started.status_code == 202 and started.json()["job"]["lane"] == "compute"
    result = world.done(started)

    assert result["at"]["id"] == head.id and result["version"]["seq"] == head.seq + 1
    found = {one["id"]: one for one in result["questions"]}
    assert sorted(found) == ["demand_ahead", "level_ahead", "level_whatif", "levels"]
    assert found["levels"]["status"] == "validated" and found["levels"]["basis"] == "latency"
    assert found["levels"]["measured_error"] is None
    assert found["level_ahead"]["status"] == "validated"
    assert found["level_ahead"]["basis"] == "backtest" and found["level_ahead"]["model"] == NAIVE
    assert found["level_ahead"]["measured_error"] == pytest.approx(0.0, abs=1e-9)
    assert found["demand_ahead"]["status"] == "provisional"
    assert "forecast origins" in found["demand_ahead"]["reason"]
    assert found["level_whatif"]["status"] == "validated"
    assert found["level_whatif"]["measured_error"] <= 1000.0
    written = world.twin.store.versions(result["version"]["id"])[0]
    assert written.cause == "registration" and written.extra["measured"] == sorted(found)
    stored = world.store_rows("TwinQuestion")["twin_question:levels"]
    assert stored["status"] == "validated" and stored["basis"] == "latency"
    assert stored["reason"] is None and stored["measured_at"] == head.t

    again = world.post("/questions/measure", json={}, headers={"Idempotency-Key": "measure-1"})
    assert again.status_code == 200
    assert again.json()["job"]["id"] == started.json()["job"]["id"]
    world.refused(
        world.post("/questions/measure", json={"questions": ["nothing"]}), 404, "question.not_found"
    )
    one = world.done(world.post("/questions/measure", json={"questions": ["levels"]}))
    assert [found["id"] for found in one["questions"]] == ["levels"]

    after = world.post("/questions/levels/answer", json={"uids": ["node:T1"]}).json()
    assert after["provisional"] is False and after["reasons"] == []
    assert [row["uid"] for row in after["value"]] == ["node:T1"]
    assert after["value"][0]["value"] == level("T1", LAST) and after["unit"] == "m"
    ahead = world.post("/questions/level_ahead/answer").json()
    assert ahead["model"] == NAIVE and len(ahead["value"]) == 8 and ahead["provisional"] is False
    world.refused(world.post("/questions/demand_ahead/answer"), 409, "model.none_active")
    world.refused(world.post("/questions/level_whatif/answer"), 404, "function.not_found")
    world.refused(world.post("/questions/nothing/answer"), 404, "question.not_found")
    assert world.ok("/meta/health")["compute"]["questions"] == {
        "validated": 3,
        "provisional": 1,
        "failing": 0,
    }
