"""Nothing secret leaves the API of a live twin: not the token, not the store's password, not
where a source lives, in no response and no log line."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from api_testkit import TOKEN
from live_api_testkit import PLANT_ENV, LiveApi, plant, records
from live_testkit import at

MakeLive = Callable[..., LiveApi]


def test_no_route_of_a_live_twin_tells_a_secret_or_where_a_source_lives(
    live_api: MakeLive, plant_dir: Path, log_lines: Path
):
    sources, folders = plant(plant_dir)
    live = live_api(sources)
    first = live.run({}, **{"Idempotency-Key": "first"})
    # A source whose folder is gone fails for real, with the connector's own error.
    shutil.rmtree(folders["lab"].path)
    live.clock.set(at(31))
    second = live.run({})
    assert second["result"]["status"] == "partial"
    assert live.sources()["lab"]["status"] == "failing"

    uid = first["result"]["run"]
    gate = live.plant.hold("scada")
    held = live.post("/api/v1/sync/runs", json={})
    assert gate.reached.wait(60)
    answers = [
        held,
        live.post("/api/v1/sync/runs", json={"sources": ["lab"]}),
        live.post("/api/v1/sync/runs", json={"sources": ["nowhere"]}),
        live.get("/api/v1/meta/health"),
        live.get("/api/v1/sync/sources"),
        live.get("/api/v1/sync/runs"),
        live.get(f"/api/v1/sync/runs/{uid}"),
        live.get(f"/api/v1/sync/runs/{uid}/rejects"),
        live.get("/api/v1/lifecycle/jobs"),
        live.get(f"/api/v1/lifecycle/jobs/{first['id']}"),
        live.get(f"/api/v1/lifecycle/jobs/{second['id']}"),
    ]
    gate.open()
    live.wait(held.json()["job"]["id"])
    assert [answer.status_code for answer in answers] == [
        202,
        409,
        400,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
    ]

    password = live.settings.store.password
    secrets = [str(plant_dir), TOKEN, password]
    for answer in answers:
        text = answer.text
        for secret in secrets:
            assert secret not in text, answer.request.url
        assert PLANT_ENV not in text, answer.request.url
        assert '"location"' not in text, answer.request.url

    lines = records(log_lines)
    assert lines
    for line in lines:
        text = json.dumps(line)
        for secret in secrets:
            assert secret not in text, line
        # The connector names a source's location in the form the manifest writes it, with
        # its variables unfilled; the API never names it at all.
        if line["context"].startswith("api"):
            assert PLANT_ENV not in text, line
    assert os.environ[PLANT_ENV] == str(plant_dir)
