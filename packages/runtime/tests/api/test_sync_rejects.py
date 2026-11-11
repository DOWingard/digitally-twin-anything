"""The rows a sync run set aside, read back through the API from the file its record names."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from dta_runtime.api.rejectfile import locate
from live_api_testkit import LiveApi, hidden, plant, problem_code, records
from live_testkit import Folder, at, speeds_source

MakeLive = Callable[..., LiveApi]
RUNS = "/api/v1/sync/runs"
# Twelve speeds beyond the highest the ontology allows, then one that holds.
SPEEDS = [("PU1", at(minute), 5.0 + minute) for minute in range(1, 13)] + [("PU1", at(13), 1.0)]
# A level above the tank's highest, the last of its interval.
LEVELS = [("T1", at(5), 9.5), ("T2", at(7), 2.5)]


def messy_plant(plant_dir: Path) -> dict[str, Any]:
    sources, _ = plant(plant_dir, "registry", "scada", levels=LEVELS)
    telemetry = speeds_source(Folder(plant_dir / "telemetry"), SPEEDS)
    return {**sources, "telemetry": hidden(telemetry, "telemetry")}


def file_lines(live: LiveApi, location: str) -> list[dict[str, Any]]:
    text = (live.settings.artifacts_dir / location).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def every_page(live: LiveApi, uid: str, **params: Any) -> tuple[list[dict[str, Any]], int]:
    """Every line, page after page, and how many pages it took."""
    lines: list[dict[str, Any]] = []
    pages = 0
    cursor = None
    while True:
        query = {**params, **({"cursor": cursor} if cursor else {})}
        page = live.ok(f"{RUNS}/{uid}/rejects", params=query)
        pages += 1
        lines.extend(page["data"])
        cursor = page["next_cursor"]
        if cursor is None:
            return lines, pages


@pytest.fixture
def messy(live_api: MakeLive, plant_dir: Path) -> tuple[LiveApi, dict[str, Any]]:
    live = live_api(messy_plant(plant_dir))
    done = live.run({})
    assert done["state"] == "succeeded"
    return live, done["result"]


def test_the_rows_set_aside_are_paged_in_file_order(messy: tuple[LiveApi, dict[str, Any]]):
    live, report = messy
    uid, location = report["run"], report["rejects_location"]
    assert location and report["rows_rejected"] == 13
    written = file_lines(live, location)
    assert len(written) == 13

    first = live.ok(f"{RUNS}/{uid}/rejects")
    assert first == {"data": written, "next_cursor": None, "total": 13, "skipped": 0}
    assert first["data"][0]["rule"] in ("range", "check")

    lines, pages = every_page(live, uid, limit=5)
    assert (lines, pages) == (written, 3)

    by_rule, _ = every_page(live, uid, rule="range", limit=4)
    assert by_rule == [line for line in written if line["rule"] == "range"]
    assert len(by_rule) == 12
    assert all(line["mapping"] == "telemetry.speeds" for line in by_rule)
    checked = live.ok(f"{RUNS}/{uid}/rejects", params={"mapping": "scada.levels"})
    assert (checked["total"], [line["rule"] for line in checked["data"]]) == (1, ["check"])
    assert checked["data"][0]["uid"] == "node:T1"
    none = live.ok(f"{RUNS}/{uid}/rejects", params={"rule": "check", "mapping": "telemetry.speeds"})
    assert none == {"data": [], "next_cursor": None, "total": 0, "skipped": 0}
    # The total counts what passes the filters, whatever the page holds.
    small = live.ok(f"{RUNS}/{uid}/rejects", params={"rule": "range", "limit": 2})
    assert (len(small["data"]), small["total"]) == (2, 12)

    # A cursor keeps to the question it was issued for.
    other = live.get(
        f"{RUNS}/{uid}/rejects", params={"rule": "check", "cursor": small["next_cursor"]}
    )
    assert (other.status_code, problem_code(other)) == (400, "api.bad_cursor")
    forged = live.get(f"{RUNS}/{uid}/rejects", params={"cursor": "eyJrIjoicmVqZWN0cyJ9"})
    assert (forged.status_code, problem_code(forged)) == (400, "api.bad_cursor")
    assert live.get(f"{RUNS}/{uid}/rejects", params={"limit": 0}).status_code == 400
    assert live.get(f"{RUNS}/{uid}/rejects", params={"limit": 5001}).status_code == 400


def test_lines_that_are_not_objects_are_skipped_and_counted(
    messy: tuple[LiveApi, dict[str, Any]],
):
    live, report = messy
    uid, location = report["run"], report["rejects_location"]
    path = live.settings.artifacts_dir / location
    first = live.ok(f"{RUNS}/{uid}/rejects", params={"limit": 3})
    with path.open("a", encoding="utf-8") as handle:
        handle.write('not json\n[1, 2]\n\n{"rule": "range", "mapping": "telemetry.speeds"}\n')

    found = live.ok(f"{RUNS}/{uid}/rejects", params={"rule": "range"})

    assert (found["total"], found["skipped"]) == (13, 2)
    assert found["data"][-1] == {"rule": "range", "mapping": "telemetry.speeds"}
    # A cursor of the file before it changed is refused, not misread.
    stale = live.get(f"{RUNS}/{uid}/rejects", params={"limit": 3, "cursor": first["next_cursor"]})
    assert (stale.status_code, problem_code(stale)) == (400, "api.bad_cursor")


def test_a_run_without_rows_set_aside_gives_none(messy: tuple[LiveApi, dict[str, Any]]):
    live, _ = messy
    live.clock.set(at(31))
    quiet = live.run({})["result"]
    assert (quiet["rows_rejected"], quiet["rejects_location"]) == (0, None)

    found = live.ok(f"{RUNS}/{quiet['run']}/rejects")

    assert found == {"data": [], "next_cursor": None, "total": 0, "skipped": 0}
    missing = live.get(f"{RUNS}/twin_sync_run:20260105T000000Z-0000/rejects")
    assert (missing.status_code, problem_code(missing)) == (404, "sync.run_not_found")


def test_a_rejects_file_that_is_gone_is_not_found(
    messy: tuple[LiveApi, dict[str, Any]], log_lines: Path
):
    live, report = messy
    path = live.settings.artifacts_dir / report["rejects_location"]
    path.rename(path.with_suffix(".moved"))

    response = live.get(f"{RUNS}/{report['run']}/rejects")

    assert (response.status_code, problem_code(response)) == (404, "sync.rejects_missing")
    (warned,) = [found for found in records(log_lines) if "rejects file" in found["message"]]
    assert (warned["level"], warned["data"]["error_type"]) == ("warn", "FileNotFoundError")


def test_a_rejects_file_outside_the_artifacts_directory_is_refused(
    live_api: MakeLive, plant_dir: Path, tmp_path: Path, log_lines: Path
):
    # The run writes its file through a link that leads out of the artifacts directory.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "rejects").symlink_to(elsewhere, target_is_directory=True)
    live = live_api(messy_plant(plant_dir))
    report = live.run({})["result"]
    assert (elsewhere / Path(report["rejects_location"]).name).is_file()

    response = live.get(f"{RUNS}/{report['run']}/rejects")

    assert (response.status_code, problem_code(response)) == (404, "sync.rejects_missing")
    assert str(elsewhere) not in response.text
    refused = [found for found in records(log_lines) if "outside" in found["message"]]
    assert len(refused) == 1 and refused[0]["level"] == "warn"


@pytest.mark.parametrize(
    "location",
    ["../outside.jsonl", "rejects/../../outside.jsonl", "/etc/hostname", "", ".", "rejects/.."],
)
def test_a_location_that_leads_out_is_never_followed(tmp_path: Path, location: str):
    root = tmp_path / "artifacts"
    (root / "rejects").mkdir(parents=True)
    assert locate(root, location) is None


def test_a_location_inside_is_followed(tmp_path: Path):
    root = tmp_path / "artifacts"
    (root / "rejects").mkdir(parents=True)
    assert locate(root, "rejects/a.jsonl") == (root / "rejects" / "a.jsonl").resolve()


def test_a_large_file_is_read_as_a_stream(messy: tuple[LiveApi, dict[str, Any]]):
    live, report = messy
    uid = report["run"]
    path = live.settings.artifacts_dir / report["rejects_location"]
    count = 200_000
    line = {
        "run": uid,
        "source": "telemetry",
        "mapping": "telemetry.speeds",
        "type": "Pump",
        "uid": "link:PU1",
        "property": "speed",
        "rule": "range",
        "message": "above the highest value allowed",
        "t": "2026-01-05T00:01:00Z",
    }
    with path.open("w", encoding="utf-8") as handle:
        for number in range(count):
            other = {**line, "rule": "check"} if number % 4 == 0 else line
            handle.write(json.dumps({**other, "n": number}) + "\n")

    started = time.perf_counter()
    first = live.ok(f"{RUNS}/{uid}/rejects", params={"rule": "range", "limit": 5000})
    counted_s = time.perf_counter() - started
    assert (first["total"], len(first["data"])) == (150_000, 5000)
    assert first["data"][0]["n"] == 1

    cursor, seen, last = first["next_cursor"], 5000, None
    started = time.perf_counter()
    pages = 0
    while cursor is not None and pages < 5:
        page = live.ok(
            f"{RUNS}/{uid}/rejects", params={"rule": "range", "limit": 5000, "cursor": cursor}
        )
        seen += len(page["data"])
        last = page["data"][-1]["n"]
        cursor = page["next_cursor"]
        pages += 1
    paged_s = (time.perf_counter() - started) / pages
    assert seen == 30_000
    # The 30 000th line passing the filter: three of every four lines pass.
    assert last == 39_999
    print(
        f"\nrejects, {count} lines: first page {counted_s * 1000:.0f} ms, "
        f"next pages {paged_s * 1000:.0f} ms each"
    )
