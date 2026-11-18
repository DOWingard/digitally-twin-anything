"""The interface's API client and block reader against the real API: problems as the user
sees them, failures logged with route, status, code and duration but never the token,
and blocks read by cursor, a jump ahead included."""

from __future__ import annotations

import json

import pytest
from dta_runtime.ui.client import ApiClient, ApiProblem
from dta_runtime.ui.paging import PagedReader
from ui_testkit import READINGS, free_port, release_port

READINGS_PATH = "/api/v1/objects/Reading"
ROWS = 6 + READINGS


def _logged(stack, message):
    found = []
    for line in stack.log_file.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("context") == "ui.client" and record.get("message") == message:
            found.append(record)
    return found


def test_a_refused_call_is_a_problem_and_is_logged_without_the_token(stack):
    client = ApiClient(stack.api_url, stack.token)
    try:
        before = len(_logged(stack, "API call refused"))
        with pytest.raises(ApiProblem) as caught:
            client.get(
                "/objects/{type}",
                path={"type": "Reading"},
                params={"where": ["sequence:eq:2.5"], "limit": 5},
            )
    finally:
        client.close()
    problem = caught.value
    assert (problem.status, problem.code, problem.title) == (400, "store.bad_filter", "Bad filter")
    assert problem.trace_id and len(problem.trace_id) == 32
    records = _logged(stack, "API call refused")[before:]
    assert records, "the refused call was not logged"
    data = records[-1]["data"]
    assert data["route"] == "/objects/{type}"
    assert data["status"] == 400
    assert data["code"] == "store.bad_filter"
    assert data["duration_ms"] >= 0
    assert data["api_trace_id"] == problem.trace_id
    # The filter's value is the user's; only parameter names are logged. The record's
    # timestamp and duration are numbers of their own that may hold the same digits.
    assert "sequence:eq" not in json.dumps(records[-1])
    assert "2.5" not in json.dumps(
        {key: value for key, value in data.items() if key != "duration_ms"}
    )
    assert stack.token not in stack.log_file.read_text(encoding="utf-8")


def test_an_unreachable_api_is_a_problem_and_is_logged(stack):
    port = free_port()
    client = ApiClient(f"http://127.0.0.1:{port}", stack.token)
    try:
        before = len(_logged(stack, "API call failed: the API cannot be reached"))
        with pytest.raises(ApiProblem) as caught:
            client.get("/meta/health")
    finally:
        client.close()
        release_port(port)
    assert caught.value.code == "ui.api_unreachable"
    records = _logged(stack, "API call failed: the API cannot be reached")[before:]
    assert records and records[-1]["level"] == "error"
    assert records[-1]["data"]["route"] == "/meta/health"
    assert "duration_ms" in records[-1]["data"]


def test_blocks_are_read_by_cursor_and_a_jump_reads_forward_once(stack):
    client = ApiClient(stack.api_url, stack.token)
    head = stack.history.versions[max(stack.history.versions)].id
    try:
        whole = client.get(
            "/objects/{type}", path={"type": "Reading"}, params={"at": head, "limit": 1000}
        )["data"]
        assert len(whole) == ROWS
        reader = PagedReader(client)
        params = {"at": head, "sort": None, "where": None}

        mark = stack.api_requests.mark()
        block = reader.block(
            "/objects/{type}", path={"type": "Reading"}, params=params, start=300, end=400
        )
        assert block.rows == whole[300:400]
        assert block.last_row is None
        jumped = stack.api_requests.since(mark, READINGS_PATH)
        assert [(r.value("limit"), r.value("cursor")) for r in jumped] == [("400", None)]

        mark = stack.api_requests.mark()
        block = reader.block(
            "/objects/{type}", path={"type": "Reading"}, params=params, start=400, end=500
        )
        assert block.rows == whole[400:]
        assert block.last_row == ROWS
        following = stack.api_requests.since(mark, READINGS_PATH)
        assert len(following) == 1
        assert following[0].value("limit") == "100" and following[0].value("cursor")

        # A block already passed is read from its own cursor, not from the start.
        mark = stack.api_requests.mark()
        assert (
            reader.block(
                "/objects/{type}", path={"type": "Reading"}, params=params, start=400, end=450
            ).rows
            == whole[400:450]
        )
        again = stack.api_requests.since(mark, READINGS_PATH)
        assert len(again) == 1 and again[0].value("limit") == "50"
    finally:
        client.close()
