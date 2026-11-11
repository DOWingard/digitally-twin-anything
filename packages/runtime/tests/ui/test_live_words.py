"""How a live twin is told in words, and what the browser-side callbacks rely on; no
browser and no store needed."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from dta_runtime import ui
from dta_runtime.ui import header, ids
from dta_runtime.ui.history import next_log_head
from dta_runtime.ui.sync import run_outcome
from dta_runtime.ui.text import duration_text, minute_text, seconds_text

CLIENTSIDE = Path(ui.__file__).parent / "assets" / "clientside.js"


def words(component: Any) -> str:
    """The text a component tree shows."""
    if component is None:
        return ""
    if isinstance(component, str | int | float):
        return str(component)
    if isinstance(component, list | tuple):
        return "".join(words(part) for part in component)
    return words(getattr(component, "children", None))


def test_the_browser_side_callbacks_name_the_stores_and_the_pause_the_server_uses():
    text = CLIENTSIDE.read_text(encoding="utf-8")
    assert re.search(r"var SETTLE_MS = (\d+);", text).group(1) == str(header.SETTLE_MS)
    targets = set(re.findall(r'settle\("\w+", "([\w-]+)", "data"', text))
    assert targets == {ids.SETTLED, ids.GRAPH_SEARCH_TEXT}


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0 s"), (45, "45 s"), (720, "12 min"), (3660, "1 h 1 min"), (90061, "1 d 1 h")],
)
def test_ages_are_told_in_their_largest_unit_and_the_next(seconds: float, text: str):
    assert seconds_text(seconds) == text


def test_times_and_limits_as_the_api_writes_them():
    assert minute_text("2026-01-19T00:15:42Z") == "2026-01-19 00:15 UTC"
    assert duration_text("PT1H") == "1 h"
    assert duration_text("PT7M30S") == "7 min 30 s"
    assert duration_text("not a duration") == "not a duration"


SYNC = {
    "interval": "PT15M",
    "staleness": "PT1H",
    "freshness_s": 720.0,
    "fresh": True,
    "questions": [
        {"id": "levels", "staleness": "PT1H", "freshness_s": 720.0, "fresh": True, "source": "a"}
    ],
    "sources": [],
    "last_run": {
        "uid": "twin_sync_run:1",
        "status": "succeeded",
        "clock_at": "2026-01-19T00:15:00Z",
        "finished_at": "2026-01-19T00:15:00Z",
    },
    "job": None,
}
CLOCK = {"kind": "source:telemetry", "available": True, "now": "2026-01-19T00:15:00Z"}


def test_the_live_line_tells_time_age_and_last_run():
    line = words(header.live_line({"clock": CLOCK, "sync": SYNC}))
    assert line == (
        "twin time 2026-01-19 00:15 UTC (clock: source telemetry) · data 12 min old (limit 1 h)"
        " · last run succeeded at 2026-01-19 00:15 UTC"
    )


def test_the_live_line_marks_stale_data_with_the_questions_behind():
    behind = {
        **SYNC,
        "freshness_s": 4000.0,
        "fresh": False,
        "questions": [
            {
                "id": "levels",
                "staleness": "PT1H",
                "freshness_s": 4000.0,
                "fresh": False,
                "source": "scada",
            },
            {
                "id": "flows",
                "staleness": "PT2H",
                "freshness_s": 4000.0,
                "fresh": True,
                "source": "scada",
            },
        ],
        "last_run": {**SYNC["last_run"], "status": "partial"},
        "job": {"id": "j", "state": "running"},
    }
    line = words(header.live_line({"clock": CLOCK, "sync": behind}))
    assert (
        "data 1 h 6 min old (limit 1 h) stale: not fresh levels "
        "(1 h 6 min old, limit 1 h, waits for scada)"
    ) in line
    assert "flows" not in line
    assert line.endswith("last run partial at 2026-01-19 00:15 UTC · run in progress")


def test_the_live_line_without_a_clock_without_a_limit_and_for_a_static_twin():
    blind = {**SYNC, "freshness_s": None, "fresh": None}
    down = {"kind": "source:telemetry", "available": False, "now": None}
    line = words(header.live_line({"clock": down, "sync": blind}))
    assert line.startswith(
        "clock unavailable (clock: source telemetry) · data age unknown (limit 1 h)"
    )
    open_ended = {**SYNC, "staleness": None, "questions": []}
    assert "old" not in words(header.live_line({"clock": CLOCK, "sync": open_ended}))
    assert header.live_line({"clock": {"kind": "fixed"}, "sync": None}) == []
    assert header.sync_key(None) == {"live": False}


def test_a_finished_run_is_told_by_how_it_ended():
    succeeded = {
        "state": "succeeded",
        "result": {"status": "succeeded", "versions_written": 1, "rows_written": 8},
    }
    assert run_outcome(succeeded) == {
        "status": "succeeded",
        "text": "The run succeeded: 1 version and 8 rows written.",
    }
    partial = {
        "state": "succeeded",
        "result": {
            "status": "partial",
            "versions_written": 2,
            "rows_written": 1,
            "rows_rejected": 3,
            "sources": {
                "lab": {
                    "status": "failing",
                    "error": {"code": "source.unavailable", "message": "down"},
                },
                "scada": {"status": "ok", "error": None},
            },
        },
    }
    assert run_outcome(partial)["text"] == (
        "The run was partial: 2 versions and 1 row written, 3 rows set aside. "
        "Failing: lab (source.unavailable: down)"
    )
    failed = {
        "state": "failed",
        "error": {"code": "sync.source_failed", "message": "every source failed"},
        "result": None,
    }
    assert run_outcome(failed) == {
        "status": "failed",
        "text": "The run failed: sync.source_failed: every source failed",
    }


def head(seq: int) -> dict[str, Any]:
    return {"head": {"id": f"v{seq}", "seq": seq}}


def test_the_commit_log_moves_to_a_new_head_only_with_the_selected_version():
    known = {"id": "v5", "seq": 5}
    # First seen, or the twin's history went back (a twin installed again).
    assert next_log_head(head(5), None, None) == known
    assert next_log_head(head(3), {"seq": 3}, known) == {"id": "v3", "seq": 3}
    # A new head the page stays away from leaves the log where it is.
    assert next_log_head(head(7), {"seq": 4}, known) is None
    assert next_log_head(head(7), {"seq": 5}, known) is None
    # Following or jumping: the selected version went past the log's head.
    assert next_log_head(head(7), {"seq": 7}, known) == {"id": "v7", "seq": 7}
    assert next_log_head(head(5), {"seq": 5}, known) is None
