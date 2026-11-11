"""Full-size timing of the HTTP connector (run with `-m scale`).

The threshold is fixed here, before measuring: 200,000 rows of 4 columns in pages
of 10,000 from a local interface, read and conformed to declared columns.
"""

from __future__ import annotations

import json
import os
import time
from datetime import timedelta

import pyarrow as pa
import pytest
from dta_core.sources import BATCH_ROWS
from dta_http_testkit import T0, Api, Call, Reply, connect, iso

ROWS = 200_000
PAGE_ROWS = 10_000
SENSORS = 100
READ_LIMIT_S = 20.0


def _pages() -> list[bytes]:
    """Every page's body, serialised up front so that the server costs little during the read."""
    pages = []
    for number in range(ROWS // PAGE_ROWS):
        start = number * PAGE_ROWS
        rows = [
            {
                "sensor_id": f"S-{index % SENSORS:03d}",
                "timestamp": iso(T0 + timedelta(seconds=10 * (index // SENSORS))),
                "value": round(50.0 + (index % 997) * 0.01, 2),
                "quality": "good" if index % 50 else "suspect",
            }
            for index in range(start, start + PAGE_ROWS)
        ]
        cursor = str(number + 1) if start + PAGE_ROWS < ROWS else None
        pages.append(json.dumps({"data": rows, "next_cursor": cursor}).encode())
    return pages


@pytest.mark.scale
def test_read_two_hundred_thousand_rows(api: Api) -> None:
    pages = _pages()

    def route(call: Call) -> Reply:
        return Reply(pages[int(call.arg("cursor") or 0)])

    api.route("/r", route)
    dataset = {
        "path": "/r",
        "params": {"limit": PAGE_ROWS},
        "records": "data",
        "paging": {"type": "cursor", "cursor_path": "next_cursor"},
        "columns": {
            "sensor_id": "string",
            "timestamp": "timestamp[us, tz=UTC]",
            "value": "double",
            "quality": "string",
        },
    }
    with connect(api, {"r": dataset}) as connector:
        load_before = os.getloadavg()
        started = time.perf_counter()
        tables = list(connector.read("r"))
        read_s = time.perf_counter() - started
        load_after = os.getloadavg()

    print(
        f"\nhttp scale: read {ROWS} rows in {len(pages)} pages in {read_s:.2f} s "
        f"(limit {READ_LIMIT_S} s), load average before {load_before}, after {load_after}"
    )
    assert sum(table.num_rows for table in tables) == ROWS
    assert max(table.num_rows for table in tables) <= BATCH_ROWS
    assert tables[0].schema.field("timestamp").type == pa.timestamp("us", tz="UTC")
    assert len(api.calls_to("/r")) == ROWS // PAGE_ROWS
    assert read_s <= READ_LIMIT_S
