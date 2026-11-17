"""Runs of shipped code: what comes back, what the run reads of the twin, and the
requests the worker refuses before anything runs."""

from __future__ import annotations

import json

import httpx
from dta_runtime.protocol import FrameRef
from worker_testkit import (
    AUTH,
    FRAME,
    Worker,
    fit_body,
    predict_body,
    problem_of,
    table_of,
    trace_id,
)


def test_a_json_result_comes_back_whole_with_its_duration_and_log_lines(worker: Worker):
    body = fit_body("log", lines=3)
    result = worker.ok(body)
    assert result["state"] == "0" * 64 and result["series"] == 0
    assert result["duration_ms"] > 0
    lines = [line for line in result["logs"] if line["context"] == "plant.model"]
    assert [line["data"]["line"] for line in lines] == [0, 1, 2]
    assert all(line["trace_id"] == body["trace_id"] for line in lines)
    assert all(line["level"] == "info" and line["message"] == "fitting" for line in lines)
    assert {"timestamp", "level", "context", "message", "trace_id", "error", "stack"} <= set(
        lines[0]
    )
    # Below info nothing is kept.
    assert not [line for line in result["logs"] if line["level"] == "debug"]


def test_a_table_result_comes_back_as_arrow_with_the_result_in_its_metadata(worker: Worker):
    body = predict_body("table")
    table, result = table_of(worker.run(body))
    assert table.to_pydict() == {"unique_id": ["T1", "T1"], "mean": [1.5, 2.5]}
    # The handler's own metadata is kept beside the result.
    assert table.schema.metadata[b"kept"] == b"yes"
    assert result["rows"] == 2 and result["series"] == 1
    assert result["duration_ms"] > 0
    assert any(line["message"] == "run started" for line in result["logs"])


def test_log_lines_past_two_hundred_are_dropped_and_counted(worker: Worker):
    result = worker.ok(fit_body("log", lines=250))
    logs = result["logs"]
    assert len(logs) == 200
    assert logs[-1]["message"] == "log lines dropped"
    assert logs[-1]["level"] == "warn"
    # The first lines are kept: the run's own start, maybe its handler's import, then its own.
    fitting = [line["data"]["line"] for line in logs[:-1] if line["message"] == "fitting"]
    assert fitting == list(range(len(fitting))) and len(fitting) >= 197
    assert logs[-1]["data"] == {"dropped": 250 - len(fitting), "kept": 199}


def test_a_shipped_run_reads_the_twin_at_its_head_and_keeps_an_artifact(
    worker: Worker, twin_api
):
    head = twin_api.water.head
    table, result = table_of(worker.run(predict_body("read", limit=2)))
    # Six junctions at the head, read two to a page.
    assert sorted(table.column("uid").to_pylist()) == [f"node:J{n}" for n in range(1, 7)]
    at, branch, health, stored, back = result["notes"]
    assert (at, branch) == (head.id, head.branch)
    assert json.loads(health)["twin_id"] == "water"
    assert json.loads(health)["head"]["id"] == head.id
    assert json.loads(back) == {"at": head.id, "rows": 6}
    assert twin_api.blob(stored).read_bytes().decode() == back


def test_a_frame_is_read_from_the_api_with_the_runs_token_and_trace(worker: Worker, twin_api):
    body = predict_body("frame")
    table, result = table_of(worker.run(body))
    assert table.column("y").to_pylist() == [2.4, 2.5, 2.6]
    assert table.column("target").to_pylist() == ["Tank.level"] * 3
    seen_trace, sent = twin_api.frames[-1]
    assert seen_trace == body["trace_id"]
    assert FrameRef.model_validate(sent) == FrameRef.model_validate(FRAME)


def test_a_request_without_the_token_is_refused(worker: Worker):
    for headers in ({}, {"Authorization": "Bearer not-the-token-at-all-0000000"}, {"Authorization": "Basic x"}):
        response = worker.run(predict_body(), headers=headers)
        problem_of(response, 401, "worker.unauthorized")
        assert response.headers["www-authenticate"] == "Bearer"
    # Health needs no token.
    assert httpx.get(f"{worker.url}/healthz").json() == {"status": "ok"}
    problem_of(worker.client.get("/nowhere"), 401, "worker.unauthorized")
    problem_of(worker.client.get("/nowhere", headers=AUTH), 404, "worker.not_found")


def test_a_malformed_body_is_refused_naming_fields_never_values(worker: Worker):
    secret = "SECRET-VALUE-9f2c"
    response = worker.client.post("/run", content=b"{not json", headers=AUTH)
    problem_of(response, 400, "worker.bad_request")
    bad = {**predict_body(), "budget_s": 0, "horizon": secret}
    response = worker.run(bad)
    document = problem_of(response, 400, "worker.bad_request")
    assert secret not in response.text
    named = {".".join(error["loc"]) for error in document["errors"]}
    assert named == {"predict.budget_s", "predict.horizon"}
    assert "budget_s" in document["detail"]


def test_an_unknown_op_is_refused(worker: Worker):
    response = worker.run({"op": "train", "budget_s": 5, "trace_id": trace_id()})
    document = problem_of(response, 400, "worker.bad_request")
    assert "train" not in response.text
    assert document["errors"][0]["type"] == "union_tag_invalid"


def test_a_handler_that_cannot_be_imported_is_named(worker: Worker):
    body = {
        **fit_body(),
        "op": "backtest",
        "step": 1,
        "windows": 1,
        "metrics": ["mae"],
    }
    document = problem_of(worker.run(body), 501, "worker.handler_missing")
    assert "worker_absent_module" in document["detail"]
    assert document["trace_id"] == body["trace_id"]
    # The process that tried is fine: the next run in the same pool succeeds.
    assert worker.ok(fit_body())["state"]


def test_a_handlers_errors_pass_through_with_their_code(worker: Worker):
    document = problem_of(worker.run(predict_body("bad_state")), 422, "model.bad_state")
    assert document["detail"] == "the state was fitted to another frame"
    document = problem_of(worker.run(predict_body("refuse")), 422, "model.unknown_family")
    assert document["detail"] == "no such family here"
