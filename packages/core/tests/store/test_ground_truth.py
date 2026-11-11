"""An evolving graph is loaded commit by commit and every kind of read is checked
against truth kept outside the store. The small fixture runs always; the two
full-size ones run with `-m scale` and also assert the performance thresholds."""

from __future__ import annotations

import json
import os
import random
import statistics
import time
from collections.abc import Callable

import pytest
from dta_core.store import ChangeSet, Store, VersionRef
from dta_store_testkit import FIXTURES, Fixture, Step, at, graph_ontology, meta, prov

# Fixed before measuring. Milliseconds, except the ratio.
THRESHOLDS = {
    "point_read_asof_p95_ms": 50.0,
    "point_read_oldest_over_newest": 3.0,
    "node_table_asof_ms": 2000.0,
    "property_history_ms": 1000.0,
    "commit_400_rows_p95_ms": 1000.0,
    "branch_create_ms": 1000.0,
    "edge_projection_ms": 3000.0,
}


def _close(a: object, b: object) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and abs(float(a) - float(b)) < 1e-9
    return a == b


def _same_rows(got: dict[str, dict], want: dict[str, dict]) -> bool:
    return got.keys() == want.keys() and all(
        row.keys() == want[uid].keys() and all(_close(row[k], want[uid][k]) for k in row)
        for uid, row in got.items()
    )


def _by_uid(table) -> dict[str, dict]:
    return {row["uid"]: row for row in table.to_pylist()}


def load(store: Store, fixture: Fixture) -> tuple[list[VersionRef], list[Step], list[float]]:
    """Commit the whole fixture; version `i` of the fixture is version `i + 1` of the store."""
    versions, steps, commit_ms = [], [], []
    for index, step in fixture.commits():
        started = time.perf_counter()
        ref = store.commit(
            ChangeSet(
                upserts=step.upserts,
                deletes=step.deletes,
                provenance=prov("observed", t_valid=at(index)),
            ),
            meta("sync", at(index), run=f"run-{index}"),
        )
        commit_ms.append((time.perf_counter() - started) * 1000.0)
        assert ref.seq == index + 1
        versions.append(ref)
        steps.append(step)
    return versions, steps, commit_ms


def verify(store: Store, fixture: Fixture, versions: list[VersionRef], steps: list[Step]) -> None:
    rng = random.Random(11)
    for index in fixture.sample_indices:
        snapshot, version = fixture.snapshots[index], versions[index]
        for type_name in ("Site", "Asset", "Sensor", "Feeds"):
            got = _by_uid(store.table(type_name, at=version))
            assert _same_rows(got, snapshot[type_name]), f"{type_name} table at version {index}"
        # The same version named by position, by id and by time reads the same rows.
        by_seq = _by_uid(store.table("Asset", at=f"#{version.seq}", columns=["load"]))
        by_id = _by_uid(store.table("Asset", at=version.id, columns=["load"]))
        by_time = _by_uid(store.table("Asset", at=version.t.isoformat(), columns=["load"]))
        assert by_seq == by_id == by_time
        for uid in rng.sample(sorted(snapshot["Asset"]), 5):
            found = store.get(uid, at=version)
            want = snapshot["Asset"][uid]
            assert found is not None and found.type == "Asset" and found.version == version
            assert all(_close(found.values[k], v) for k, v in want.items() if k != "uid")
        assert store.get("asset:none", at=version) is None

        edges = store.edges(at=version).to_pylist()
        got_edges = {(e["src"], e["dst"], e["edge_type"], e["link_uid"]) for e in edges}
        want_edges = {(r["src"], r["dst"], "Feeds", uid) for uid, r in snapshot["Feeds"].items()}
        want_edges |= {(uid, r["site"], "Asset.site", None) for uid, r in snapshot["Asset"].items()}
        want_edges |= {
            (uid, r["monitors"], "Sensor.monitors", None) for uid, r in snapshot["Sensor"].items()
        }
        assert len(edges) == len(got_edges) and got_edges == want_edges, f"edges at {index}"
        links = store.edges(["Feeds"], at=version)
        assert links.num_rows == len(snapshot["Feeds"])

    # Property history: every version in which a tracked asset's load changed.
    for uid, truth in fixture.tracked.items():
        history = store.history(uid, ["load"]).to_pylist()
        got = [(row["_seq"], row["load"]) for row in history]
        want, previous = [], None
        for index, value in truth:
            if value != previous:
                want.append((index + 1, value))
            previous = value
        assert len(got) == len(want) and all(
            g[0] == w[0] and _close(g[1], w[1]) for g, w in zip(got, want, strict=True)
        ), f"history of {uid}"
        assert history[0]["_change"] == "added"
        assert [row["_version"] for row in history] == [versions[seq - 1].id for seq, _ in got]

    # Diff between the first and the last sampled version.
    first, last = fixture.sample_indices[0], fixture.sample_indices[-1]
    difference = store.diff(versions[first], versions[last])
    for type_name in ("Asset", "Sensor", "Feeds"):
        a, b = fixture.snapshots[first][type_name], fixture.snapshots[last][type_name]
        want_kinds = {}
        for uid in a.keys() | b.keys():
            if uid not in a:
                want_kinds[uid] = "added"
            elif uid not in b:
                want_kinds[uid] = "removed"
            elif a[uid] != b[uid]:
                want_kinds[uid] = "modified"
        rows = difference[type_name].to_pylist() if type_name in difference else []
        got_kinds = {row["uid"]: row["_change"] for row in rows}
        # A row rewritten with the same values still differs in when it was observed.
        assert {u: k for u, k in got_kinds.items() if k != "modified"} == {
            u: k for u, k in want_kinds.items() if k != "modified"
        }, f"diff of {type_name}"
        assert {u for u, k in want_kinds.items() if k == "modified"} <= {
            u for u, k in got_kinds.items() if k == "modified"
        }
        for row in rows:
            if row["_change"] == "modified" and row["uid"] in want_kinds:
                before = {k: v for k, v in row["_before"].items()}
                assert _same_rows(
                    {row["uid"]: {"uid": row["uid"], **before}}, {row["uid"]: a[row["uid"]]}
                )
    assert "Site" not in difference

    # Changes of the link type over a run of versions, against the steps that made them.
    low, high = fixture.sample_indices[1], fixture.sample_indices[-2]
    changed = store.changes("Feeds", f"#{low + 1}..#{high + 1}").to_pylist()
    got_changes = {(row["_seq"], row["_change"], row["uid"]) for row in changed}
    want_changes = set()
    for index in range(low, high + 1):
        want_changes |= {(index + 1, "added", row["uid"]) for row in steps[index].upserts["Feeds"]}
        want_changes |= {
            (index + 1, "removed", uid) for uid in steps[index].deletes.get("Feeds", [])
        }
    assert len(changed) == len(got_changes) and got_changes == want_changes
    assert [row["_seq"] for row in changed] == sorted(row["_seq"] for row in changed)


def test_small_fixture_matches_ground_truth(make_store: Callable[..., Store]):
    store = make_store(graph_ontology())
    fixture = Fixture(FIXTURES["smoke"])
    versions, steps, _ = load(store, fixture)
    verify(store, fixture, versions, steps)
    assert store.head() == versions[-1]
    assert [v.seq for v in store.resolve("#1..head/3")] == list(range(1, len(versions) + 1, 3))


# -- full size -----------------------------------------------------------------


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


def measure(
    store: Store, fixture: Fixture, versions: list[VersionRef], load_ms: list[float]
) -> dict:
    params = fixture.params
    rng = random.Random(params.seed + 1)
    total = len(versions)
    numbers: dict[str, float] = {}

    def point(index: int) -> float:
        uid = f"asset:a{rng.randrange(params.assets)}"
        started = time.perf_counter()
        assert store.get(uid, at=versions[index]) is not None
        return _ms(started)

    numbers["point_read_asof_p95_ms"] = _p95([point(rng.randrange(total)) for _ in range(300)])
    tenth = max(1, total // 10)
    oldest = [point(rng.randrange(tenth)) for _ in range(150)]
    newest = [point(total - 1 - rng.randrange(tenth)) for _ in range(150)]
    numbers["point_read_oldest_over_newest"] = statistics.median(oldest) / statistics.median(newest)

    samples = []
    for _ in range(6):
        started = time.perf_counter()
        rows = store.table("Asset", at=versions[rng.randrange(total)]).num_rows
        samples.append(_ms(started))
        assert rows == params.assets
    numbers["node_table_asof_ms"] = max(samples)
    numbers["node_table_rows"] = params.assets

    samples, points = [], 0
    for uid in fixture.tracked:
        for _ in range(2):
            started = time.perf_counter()
            points = store.history(uid, ["load"]).num_rows
            samples.append(_ms(started))
    numbers["property_history_ms"] = max(samples)
    numbers["property_history_points"] = points

    samples = []
    for _ in range(3):
        started = time.perf_counter()
        links = store.edges(["Feeds"], at=versions[rng.randrange(total)]).num_rows
        samples.append(_ms(started))
        assert links == params.feeds
    numbers["edge_projection_ms"] = max(samples)
    numbers["edge_projection_links"] = params.feeds
    started = time.perf_counter()
    numbers["edge_projection_all_rows"] = store.edges(at=versions[-1]).num_rows
    numbers["edge_projection_all_ms"] = _ms(started)

    samples = []
    for round_index in range(5):
        name = f"scenario-{round_index}"
        started = time.perf_counter()
        store.branch_create(name, at=versions[rng.randrange(total)])
        samples.append(_ms(started))
        store.branch_delete(name)
    numbers["branch_create_ms"] = max(samples)

    # Change sets of 400 rows: half partial updates, half whole rows.
    samples = []
    moment = total
    for _ in range(30):
        assets = [
            {"uid": f"asset:a{i}", "load": round(rng.uniform(0, 5), 3)}
            for i in rng.sample(range(params.assets), 200)
        ]
        sensors = [
            {
                "uid": f"sensor:n{i}",
                "reading": round(rng.uniform(-50, 150), 3),
                "monitors": fixture.state["Sensor"][f"sensor:n{i}"]["monitors"],
            }
            for i in rng.sample(range(params.sensors), 200)
        ]
        moment += 1
        started = time.perf_counter()
        store.commit(
            ChangeSet(
                upserts={"Asset": assets, "Sensor": sensors},
                provenance=prov("observed", t_valid=at(moment)),
            ),
            meta("sync", at(moment)),
        )
        samples.append(_ms(started))
    numbers["commit_400_rows_p95_ms"] = _p95(samples)
    numbers["load_commit_p95_ms"] = _p95(load_ms[1:])
    numbers["load_commit_rows"] = params.changed_per_commit * 2 + params.edge_churn * 2
    numbers["initial_load_s"] = load_ms[0] / 1000.0
    numbers["versions"] = total
    return numbers


@pytest.mark.scale
@pytest.mark.parametrize("name", ["wide", "long"])
def test_full_size_fixture_matches_ground_truth_within_thresholds(
    name: str, make_store: Callable[..., Store], capsys: pytest.CaptureFixture[str]
):
    store = make_store(graph_ontology())
    fixture = Fixture(FIXTURES[name])
    started = time.perf_counter()
    versions, steps, load_ms = load(store, fixture)
    load_s = time.perf_counter() - started
    verify(store, fixture, versions, steps)
    numbers = measure(store, fixture, versions, load_ms)
    numbers["load_total_s"] = load_s
    report = {
        "fixture": name,
        "numbers": {key: round(value, 3) for key, value in numbers.items()},
        "thresholds": THRESHOLDS,
        "load_average": os.getloadavg(),
    }
    with capsys.disabled():
        print(f"\n[{name}] " + json.dumps(report))
    target = os.environ.get("DTA_STORE_SCALE_REPORT")
    if target:
        with open(f"{target}.{name}.json", "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
    failed = {key: numbers[key] for key, limit in THRESHOLDS.items() if numbers[key] > limit}
    assert not failed, f"over threshold on {name}: {failed}"
