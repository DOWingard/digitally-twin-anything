"""What a live twin tells of its sources and of how far behind them it is: `/sync/sources` and
the `clock` and `sync` members of `/meta/health`."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dta_runtime.api.live import Clocks
from dta_runtime.twin import Bundle, install_twin
from live_api_testkit import PLANT_ENV, LiveApi, live_manifest, plant, records
from live_testkit import at
from runtime_testkit import water_manifest

MakeLive = Callable[..., LiveApi]
# In the manifest's order, as the twin holds it.
READING_PROPERTIES = ["observed_at", "sensor", "sequence", "value"]


def seconds(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds()


def parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def test_sources_are_listed_in_the_manifests_order_with_what_syncing_recorded(
    live_api: MakeLive, plant_dir: Path
):
    sources, _ = plant(plant_dir)
    live = live_api(sources)

    before = live.ok("/api/v1/sync/sources")

    # The manifest as the twin holds it names its sources in this order.
    manifest = live.twin.manifest()
    assert [found["id"] for found in before["data"]] == list(manifest.sources)
    assert before["clock"] == {"kind": "fixed", "available": True, "now": "2026-01-05T00:16:00Z"}
    assert before["version"]["seq"] == live.twin.store.head().seq
    found = {source["id"]: source for source in before["data"]}
    registry, scada, lab = found["registry"], found["scada"], found["lab"]
    # Installed and never synced: known, with nothing taken yet.
    assert (registry["connector"], registry["status"], registry["last_error"]) == (
        "file",
        "ok",
        None,
    )
    assert (registry["watermark"], registry["freshness_s"], registry["datasets"]) == (
        None,
        None,
        [],
    )
    assert [m["id"] for m in registry["mappings"]] == list(manifest.sources["registry"].mappings)
    assert {(m["id"], m["object_type"], m["kind"]) for m in registry["mappings"]} == {
        ("tanks", "Tank", "static"),
        ("junctions", "Junction", "static"),
        ("pumps", "Pump", "static"),
        ("sensors", "Sensor", "static"),
    }
    assert scada["mappings"] == [
        {
            "id": "levels",
            "dataset": "levels",
            "object_type": "Tank",
            "kind": "state",
            "properties": ["level"],
        }
    ]
    assert lab["mappings"] == [
        {
            "id": "readings",
            "dataset": "readings",
            "object_type": "Reading",
            "kind": "event",
            "properties": READING_PROPERTIES,
        }
    ]

    report = live.run({})["result"]
    after = live.sources()

    now = at(16)
    scada, lab = after["scada"], after["lab"]
    assert (scada["status"], scada["watermark"]) == ("ok", "2026-01-05T00:07:00Z")
    assert scada["freshness_s"] == seconds(now, at(7)) == 540.0
    assert (lab["watermark"], lab["freshness_s"]) == ("2026-01-05T00:09:00Z", 420.0)
    assert lab["last_success_at"] is not None
    assert lab["watermark"] == report["sources"]["lab"]["watermark"]
    (dataset,) = lab["datasets"]
    assert (dataset["name"], dataset["cursor_column"], dataset["cursor"], dataset["rows_read"]) == (
        "readings",
        "rev",
        "2",
        2,
    )
    assert parse(dataset["last_read_at"]) <= now
    registry = after["registry"]
    assert registry["freshness_s"] == seconds(now, parse(registry["watermark"]))
    assert {dataset["name"] for dataset in registry["datasets"]} == {
        "tanks",
        "junctions",
        "pumps",
        "sensors",
    }


def test_where_a_source_lives_is_never_shown(live_api: MakeLive, plant_dir: Path):
    sources, _ = plant(plant_dir)
    live = live_api(sources)
    live.run({})

    for path in ("/api/v1/sync/sources", "/api/v1/meta/health"):
        text = live.get(path).text
        assert str(plant_dir) not in text
        assert PLANT_ENV not in text
        assert "location" not in text


def test_a_source_dropped_from_the_manifest_is_listed_last_disabled(
    live_api: MakeLive, plant_dir: Path, water_text: str
):
    sources, _ = plant(plant_dir)
    live = live_api(sources)
    live.run({})
    hash_before = live.health()["manifest"]["hash"]

    kept = {name: found for name, found in sources.items() if name != "lab"}
    bundle = Bundle(manifest=live_manifest(water_text, kept), ontology_text=water_text)
    installed = install_twin(live.twin.store.settings, bundle, t=at(30))
    installed.twin.close()

    listed = live.ok("/api/v1/sync/sources")["data"]
    assert [found["id"] for found in listed] == ["registry", "scada", "lab"]
    dropped = listed[-1]
    assert (dropped["status"], dropped["mappings"]) == ("disabled", [])
    assert dropped["watermark"] == "2026-01-05T00:09:00Z"
    assert [dataset["name"] for dataset in dropped["datasets"]] == ["readings"]
    health = live.health()
    assert health["manifest"]["hash"] != hash_before
    assert [found["id"] for found in health["sync"]["sources"]] == ["registry", "scada"]
    # A run reads the manifest in force when it starts.
    done = live.run({})
    assert set(done["result"]["sources"]) == {"registry", "scada"}


def test_health_tells_how_fresh_the_twin_is_on_its_clock(
    live_api: MakeLive, plant_dir: Path, log_lines: Path
):
    sources, _ = plant(plant_dir)
    live = live_api(sources)
    never = live.health()["sync"]
    assert (never["interval"], never["staleness"], never["last_run"], never["job"]) == (
        "PT15M",
        "PT1H",
        None,
        None,
    )
    assert (never["freshness_s"], never["fresh"]) == (None, None)

    report = live.run({})["result"]
    health = live.health()

    sync = health["sync"]
    assert health["clock"] == {"kind": "fixed", "available": True, "now": "2026-01-05T00:16:00Z"}
    by_id = {found["id"]: found for found in sync["sources"]}
    assert list(by_id) == list(live.twin.manifest().sources)
    assert (by_id["scada"]["watermark"], by_id["scada"]["freshness_s"]) == (
        "2026-01-05T00:07:00Z",
        540.0,
    )
    assert sync["freshness_s"] == max(found["freshness_s"] for found in sync["sources"])
    assert sync["fresh"] is True
    last = sync["last_run"]
    assert (last["uid"], last["status"], last["kind"]) == (
        report["run"],
        "succeeded",
        "incremental",
    )
    assert (last["clock_at"], last["rows_written"], last["rows_rejected"], last["error"]) == (
        "2026-01-05T00:16:00Z",
        report["rows_written"],
        0,
        None,
    )
    assert last["started_at"] and last["finished_at"]
    assert sync["job"] is None

    # Exactly as stale as the contract allows is still fresh; a second more is not.
    live.clock.set(at(7) + timedelta(hours=1))
    edge = live.health()["sync"]
    assert (edge["freshness_s"], edge["fresh"]) == (3600.0, True)
    live.clock.set(at(7) + timedelta(hours=1, seconds=1))
    stale = live.health()["sync"]
    assert (stale["freshness_s"], stale["fresh"]) == (3601.0, False)
    assert live.sources()["scada"]["freshness_s"] == 3601.0

    # What depends on the head was read once for it, however often health was asked.
    head = live.twin.store.head()
    for _ in range(10):
        live.health()
    described = [
        found
        for found in records(log_lines)
        if found["message"] == "head described" and found["data"]["head_seq"] == head.seq
    ]
    assert len(described) == 1


def test_the_clock_is_built_from_the_manifest_and_rebuilt_when_it_changes(water_text: str):
    wall = water_manifest(water_text)
    clocks = Clocks(None, {})
    try:
        before = datetime.now(UTC)
        reading = clocks.read(wall, "hash-1")
        assert (reading.kind, reading.available) == ("wall", True)
        assert reading.now is not None and before <= reading.now <= before + timedelta(minutes=1)
        first = clocks.get(wall, "hash-1")
        assert clocks.get(wall, "hash-1") is first
        assert clocks.get(wall, "hash-2") is not first

        nowhere = water_manifest(water_text, runtime={"clock": "source:nowhere"})
        broken = clocks.read(nowhere, "hash-3")
        assert (broken.kind, broken.available, broken.now) == ("source:nowhere", False, None)
        assert clocks.read(nowhere, "hash-3").available is False
        assert clocks.read(wall, "hash-4").available is True
    finally:
        clocks.close()


def test_a_given_clock_is_used_as_it_is(water_text: str):
    manifest = water_manifest(water_text, runtime={"clock": "source:nowhere"})
    given = _Stopped(at(16))
    clocks = Clocks(given, {})
    assert clocks.get(manifest, "any") is given
    assert clocks.read(manifest, "any").document() == {
        "kind": "stopped",
        "available": True,
        "now": "2026-01-05T00:16:00Z",
    }
    clocks.close()
    assert given.closed == 0


class _Stopped:
    kind = "stopped"

    def __init__(self, now: datetime) -> None:
        self._now = now
        self.closed = 0

    def now(self) -> datetime:
        return self._now

    def close(self) -> None:
        self.closed += 1
