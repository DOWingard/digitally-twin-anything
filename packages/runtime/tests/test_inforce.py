"""What was in force when, worked out from two reads of the store, is what reading the
store version by version gives."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow as pa
import pytest
from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance, VersionRef
from dta_runtime import inforce
from dta_runtime.twin import Twin

T0 = datetime(2026, 1, 5, tzinfo=UTC)
STEP = timedelta(minutes=15)
TANKS = [f"node:T{index:02d}" for index in range(1, 13)]


def _tank(uid: str, level: float | None) -> dict[str, Any]:
    return {
        "uid": uid,
        "name": uid.split(":")[1],
        "elevation": 60.0,
        "min_level": 0.0,
        "max_level": 20.0,
        "level": level,
        "volume_curve": [0.0, 10.0],
        "x": 0.0,
        "y": 0.0,
    }


def _commit(twin: Twin, t: datetime, changes: ChangeSet) -> VersionRef:
    changes.provenance = Provenance(source="scada", kind=FactKind.observed, t_valid=t)
    return twin.store.commit(changes, CommitMeta(cause="sync", actor="scada", t=t))


@pytest.fixture
def history(twin: Twin) -> list[VersionRef]:
    """Tanks whose levels change, become null, disappear and come back, over 40 versions
    of which some share a time."""
    rng = random.Random(20261111)
    versions = [
        _commit(twin, T0 + STEP, ChangeSet(upserts={"Tank": [_tank(uid, 5.0) for uid in TANKS]}))
    ]
    present = set(TANKS)
    t = T0 + STEP
    for index in range(39):
        # One version in five has the time of the one before it.
        if index % 5 != 4:
            t += STEP
        upserts, deletes = [], []
        for uid in TANKS:
            draw = rng.random()
            if uid in present and draw < 0.04 and len(present) > 3:
                deletes.append(uid)
            elif uid not in present and draw < 0.3:
                upserts.append(_tank(uid, round(rng.uniform(1, 9), 2)))
            elif uid in present and draw < 0.45:
                level = None if rng.random() < 0.15 else round(rng.uniform(1, 9), 2)
                upserts.append({"uid": uid, "level": level})
        if not upserts and not deletes:
            upserts.append({"uid": TANKS[0], "level": round(rng.uniform(1, 9), 2)})
            if TANKS[0] not in present:
                upserts[-1] = _tank(TANKS[0], 4.0)
        present |= {row["uid"] for row in upserts}
        present -= set(deletes)
        changes = ChangeSet(upserts={"Tank": upserts} if upserts else {})
        if deletes:
            changes.deletes = {"Tank": deletes}
        versions.append(_commit(twin, t, changes))
    return versions


def _levels(twin: Twin, version: VersionRef, uids: list[str] | None = None) -> dict[str, Any]:
    table = twin.store.table("Tank", columns=["level"], at=version, ontology=twin.ontology)
    found = dict(
        zip(table.column("uid").to_pylist(), table.column("level").to_pylist(), strict=True)
    )
    return found if uids is None else {uid: found[uid] for uid in uids if uid in found}


def _events(twin: Twin, first: VersionRef, last: VersionRef, **more: Any) -> pa.Table:
    return inforce.events(twin.store, twin.ontology, "Tank", first, last, **more)


def test_the_events_are_the_state_at_the_first_version_and_the_changes_after_it(twin, history):
    first, last = history[3], history[30]
    events = _events(twin, first, last, properties=["level"])
    assert events.column_names == ["uid", "_seq", "_t", "_removed", "level"]
    assert inforce.properties(events) == ["level"]
    at_first = events.filter(pa.compute.equal(events.column("_seq"), first.seq))
    assert dict(
        zip(at_first.column("uid").to_pylist(), at_first.column("level").to_pylist(), strict=True)
    ) == _levels(twin, first)
    assert set(at_first.column("_t").to_pylist()) == {first.t}
    seqs = events.column("_seq").to_pylist()
    assert min(seqs) == first.seq and max(seqs) <= last.seq
    # Nothing of before the first version or after the last is among them.
    assert not any(seq < first.seq or seq > last.seq for seq in seqs)


def test_at_versions_is_the_table_at_each_version(twin, history):
    chosen = history[2:35:3]
    events = _events(twin, chosen[0], chosen[-1], properties=["level"])
    result = inforce.at_versions(events, chosen)
    assert result.column_names == ["_version", "_seq", "_t", "uid", "level"]
    rows = result.to_pylist()
    assert rows == sorted(rows, key=lambda row: (row["uid"], row["_seq"]))
    for version in chosen:
        found = {row["uid"]: row["level"] for row in rows if row["_seq"] == version.seq}
        assert found == _levels(twin, version), version.seq
        assert {row["_t"] for row in rows if row["_seq"] == version.seq} <= {version.t}


def test_at_times_is_the_table_at_the_last_version_at_or_before_each_time(twin, history):
    first, last = history[1], history[-1]
    events = _events(twin, first, last, properties=["level"])
    # Times on version times, between them, on a time two versions share, and after the last.
    times = [first.t + index * timedelta(minutes=5) for index in range(0, 110)]
    result = inforce.at_times(events, times)
    assert result.column_names == ["_t", "uid", "level"]
    rows = result.to_pylist()
    assert rows == sorted(rows, key=lambda row: (row["uid"], row["_t"]))
    in_range = [version for version in history if first.seq <= version.seq <= last.seq]
    shared = 0
    for moment in times:
        at_or_before = [version for version in in_range if version.t <= moment]
        version = max(at_or_before, key=lambda found: found.seq)
        shared += sum(1 for found in at_or_before if found.t == version.t) > 1
        found = {row["uid"]: row["level"] for row in rows if row["_t"] == moment}
        assert found == _levels(twin, version), (moment, version.seq)
    assert shared, "no asked time fell on a time two versions share"


def test_a_time_before_the_first_version_has_no_row(twin, history):
    first, last = history[10], history[20]
    events = _events(twin, first, last, properties=["level"])
    result = inforce.at_times(events, [first.t - STEP, first.t])
    assert set(result.column("_t").to_pylist()) == {first.t}


def test_only_the_objects_asked_for(twin, history):
    wanted = TANKS[2:5]
    first, last = history[0], history[-1]
    events = _events(twin, first, last, properties=["level"], uids=wanted)
    assert set(inforce.objects(events).to_pylist()) <= set(wanted)
    result = inforce.at_versions(events, [first, history[20], last])
    for version in (first, history[20], last):
        found = {
            row["uid"]: row["level"] for row in result.to_pylist() if row["_seq"] == version.seq
        }
        assert found == _levels(twin, version, wanted)


def test_one_version_is_its_own_table(twin, history):
    version = history[7]
    events = _events(twin, version, version)
    result = inforce.at_versions(events, [version])
    assert result.num_rows == len(_levels(twin, version))
    assert "elevation" in result.column_names and "level" in result.column_names
