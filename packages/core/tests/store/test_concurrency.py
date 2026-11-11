"""Several committers at once, and readers alongside them."""

from __future__ import annotations

import json
import threading
import time

from dta_core.store import Store
from dta_store_testkit import at, changes, meta, raw_connection, sql, start_worker

SEED = {
    "Zone": [{"uid": f"zone:seed-{i}", "name": f"seed {i}"} for i in range(50)],
    "Tank": [{"uid": "node:t1", "name": "T1", "capacity": 50.0, "level": 5.0}],
}


def _snapshot(store: Store, version) -> tuple:
    zones = store.table("Zone", at=version).to_pylist()
    tank = store.get("node:t1", at=version)
    return (sorted((row["uid"], row["name"]) for row in zones), tank.values, tank.provenance)


def test_two_processes_commit_serially_while_a_pinned_reader_sees_no_change(store: Store):
    pinned = store.commit(changes(SEED), meta(t=at(1)))
    store.commit(
        changes({"Tank": [{"uid": "node:t1", "level": 6.0}]}, kind="inferred"),
        meta("derived", at(2)),
    )
    before = _snapshot(store, pinned)
    base = store.head().seq

    stop = threading.Event()
    during: list[tuple] = []
    errors: list[BaseException] = []

    def read_loop() -> None:
        try:
            while not stop.is_set():
                during.append(_snapshot(store, pinned))
        except BaseException as exc:  # reported by the assertion below
            errors.append(exc)

    reader = threading.Thread(target=read_loop, name="pinned-reader")
    reader.start()
    count = 25
    workers = [
        start_worker("commit", store.settings.database, name, str(count)) for name in ("a", "b")
    ]
    outputs = [worker.communicate(timeout=180) for worker in workers]
    stop.set()
    reader.join(timeout=60)

    for worker, (_, stderr) in zip(workers, outputs, strict=True):
        assert worker.returncode == 0, stderr[-2000:]
    positions = [json.loads(stdout) for stdout, _ in outputs]
    # Every commit of both processes succeeded, each at its own consecutive position.
    assert all(len(found) == count for found in positions)
    assert all(found == sorted(found) for found in positions)
    assert sorted(positions[0] + positions[1]) == list(range(base + 1, base + 2 * count + 1))
    # The two really did interleave rather than run one after the other.
    assert min(positions[0]) < max(positions[1]) and min(positions[1]) < max(positions[0])

    assert not errors and len(during) > 5
    assert all(seen == before for seen in during)
    assert _snapshot(store, pinned) == before

    head = store.head()
    assert head.seq == base + 2 * count
    assert store.table("Zone").num_rows == 50 + 2 * count
    runs = [version.run for version in store.versions(f"#{base + 1}..head")]
    assert runs.count("a") == count and runs.count("b") == count


def test_threads_sharing_one_store_commit_one_at_a_time(store: Store):
    store.commit(changes(SEED), meta(t=at(1)))
    base = store.head().seq
    threads, per_thread = 8, 6
    positions: dict[int, list[int]] = {index: [] for index in range(threads)}
    errors: list[BaseException] = []

    def work(index: int) -> None:
        try:
            for step in range(per_thread):
                version = store.commit(
                    changes({"Zone": [{"uid": f"zone:t{index}-{step}", "name": "x"}]}),
                    meta(t=at(2)),
                )
                positions[index].append(version.seq)
                assert store.get(f"zone:t{index}-{step}", at=version) is not None
        except BaseException as exc:
            errors.append(exc)

    pool = [threading.Thread(target=work, args=(index,)) for index in range(threads)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join(timeout=120)
    assert not errors, errors
    every = sorted(seq for found in positions.values() for seq in found)
    assert every == list(range(base + 1, base + threads * per_thread + 1))
    assert store.table("Zone").num_rows == 50 + threads * per_thread


def test_readers_are_not_held_up_by_a_commit_in_progress(store: Store):
    first = store.commit(changes(SEED), meta(t=at(1)))
    paused, resume = threading.Event(), threading.Event()

    def pause(step: str) -> None:
        if step == "written":
            paused.set()
            assert resume.wait(timeout=60)

    store._fault = pause
    done: list[int] = []
    writer = threading.Thread(
        target=lambda: done.append(
            store.commit(changes({"Zone": [{"uid": "zone:new", "name": "new"}]}), meta(t=at(2))).seq
        )
    )
    writer.start()
    try:
        assert paused.wait(timeout=30)
        # The committer holds its lock and an open transaction with the row written.
        started = time.perf_counter()
        assert store.head() == first
        assert store.table("Zone").num_rows == 50
        assert store.get("zone:new") is None
        assert store.history("zone:seed-1").num_rows == 1
        assert time.perf_counter() - started < 5.0
        assert not done
    finally:
        store._fault = None
        resume.set()
        writer.join(timeout=60)
    assert done == [2]
    assert store.get("zone:new") is not None and store.table("Zone").num_rows == 51
    assert store.table("Zone", at=first).num_rows == 50


def test_a_reader_holding_a_transaction_open_does_not_hold_up_a_commit(store: Store):
    store.commit(changes(SEED), meta(t=at(1)))
    reader = raw_connection(store.settings)
    try:
        sql(reader, "START TRANSACTION")
        assert sql(reader, "SELECT COUNT(*) FROM `zone`") == ((50,),)
        started = time.perf_counter()
        store.commit(changes({"Zone": [{"uid": "zone:new", "name": "new"}]}), meta(t=at(2)))
        assert time.perf_counter() - started < 5.0
        # The open transaction keeps seeing what it started with.
        assert sql(reader, "SELECT COUNT(*) FROM `zone`") == ((50,),)
        sql(reader, "ROLLBACK")
        assert sql(reader, "SELECT COUNT(*) FROM `zone`") == ((51,),)
    finally:
        reader.close()
