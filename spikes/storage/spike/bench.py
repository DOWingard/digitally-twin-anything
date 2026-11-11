"""Load a fixture into one engine, verify it against ground truth, and time each operation.

Gate thresholds are fixed here before any measurement is taken, so an engine
passes or fails on numbers chosen in advance.
"""

from __future__ import annotations

import json
import random
import statistics
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

import pyarrow as pa
import rustworkx as rx

from .config import Config
from .engines.base import Engine
from .engines.dolt import DoltEngine
from .engines.terminus import TerminusEngine
from .fixture import FIXTURES, ChangeSet, Fixture
from .log import RUN_ID, Log
from .ontology import Ontology

log = Log("bench")

ENGINES: dict[str, Callable[[Config, Ontology], Engine]] = {
    "dolt": DoltEngine,
    "terminusdb": TerminusEngine,
}

# Hard gates. Latencies are p95 in milliseconds, measured from the client.
GATES = {
    "point_read_asof_ms": 50.0,
    "history_scaling_ratio": 3.0,  # median read in the oldest quarter / newest quarter
    "node_table_asof_ms": 2000.0,
    "property_history_ms": 1000.0,
    "neighbors_k2_ms": 200.0,  # native traversal; the projection route below is the alternative
    "projection_traverse_ms": 2000.0,
    "branch_create_ms": 1000.0,
}
NATIVE_REJECTIONS = ("bad_enum", "missing_required", "dangling_ref", "wrong_type")
SOFT_COMMIT_MS = 2000.0


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _summary(samples: list[float]) -> dict:
    if not samples:
        return {"n": 0}
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {
        "n": len(ordered),
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(p95, 3),
        "max_ms": round(ordered[-1], 3),
    }


def _reachable(feeds: list[tuple[str, str]], seed: str, depth: int) -> set[str]:
    """Assets within 1..depth hops of the seed, computed outside any engine."""
    graph = rx.PyDiGraph()
    index: dict[str, int] = {}
    for src, dst in feeds:
        for uid in (src, dst):
            if uid not in index:
                index[uid] = graph.add_node(uid)
    graph.add_edges_from_no_data([(index[src], index[dst]) for src, dst in feeds])
    if seed not in index:
        return set()
    lengths = rx.dijkstra_shortest_path_lengths(graph, index[seed], lambda _: 1.0)
    return {graph[node] for node, hops in lengths.items() if hops <= depth}


def _close(a: object, b: object) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


class Bench:
    def __init__(self, engine: Engine, fixture: Fixture) -> None:
        self.engine = engine
        self.fixture = fixture
        self.versions: list[str] = []
        self.rng = random.Random(fixture.params.seed + 1)
        self.ops: dict[str, dict] = {}
        self.checks: dict[str, bool] = {}
        self.notes: dict[str, object] = {}

    # -- helpers ------------------------------------------------------------

    def _guard(self, name: str, work: Callable[[], None]) -> None:
        """Run one measurement; a failure is recorded and the run continues."""
        started = time.perf_counter()
        try:
            work()
            log.info("operation measured", op=name, elapsed_ms=round(_ms(started), 1))
        except Exception as exc:
            log.error("operation failed", exc=exc, op=name, engine=self.engine.name)
            self.ops[name] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}

    def _random_version(self) -> int:
        return self.rng.randrange(len(self.versions))

    # -- load ---------------------------------------------------------------

    def load(self) -> None:
        params = self.fixture.params
        self.engine.reset()
        self.notes["unenforced_constraints"] = self.engine.install_schema()
        commit_ms: list[float] = []
        report_every = max(1, params.commits // 10)
        started_all = time.perf_counter()
        for index, changes in self.fixture.commits():
            started = time.perf_counter()
            self.versions.append(self.engine.commit(changes, f"v{index}"))
            elapsed = _ms(started)
            if index == 0:
                self.notes["initial_load_s"] = round(elapsed / 1000.0, 2)
                self.notes["initial_objects"] = changes.size()
            else:
                commit_ms.append(elapsed)
            if index % report_every == 0:
                log.info("load progress", version=index, of=params.commits, last_ms=round(elapsed))
        self.ops["commit"] = _summary(commit_ms)
        self.notes["load_total_s"] = round(time.perf_counter() - started_all, 1)
        self.notes["disk_bytes"] = self.engine.disk_bytes()
        log.info("fixture loaded", versions=len(self.versions), seconds=self.notes["load_total_s"])

    # -- correctness --------------------------------------------------------

    def verify(self) -> None:
        engine, fixture = self.engine, self.fixture
        tables_ok = objects_ok = neighbors_ok = edges_ok = True
        for index in fixture.sample_indices:
            snapshot, at = fixture.snapshots[index], self.versions[index]
            table = engine.node_table("Asset", at).to_pydict()
            got = dict(zip(table["uid"], zip(table["load"], table["status"])))
            want = {uid: (row["load"], row["status"]) for uid, row in snapshot["Asset"].items()}
            if got.keys() != want.keys() or any(
                not _close(got[u][0], want[u][0]) or got[u][1] != want[u][1] for u in want
            ):
                tables_ok = False
                log.error("node table mismatch", version=index, got=len(got), want=len(want))
            edges = engine.node_table("Feeds", at).to_pydict()
            got_edges = set(zip(edges["uid"], edges["src"], edges["dst"]))
            want_edges = {(r["uid"], r["src"], r["dst"]) for r in snapshot["Feeds"].values()}
            if got_edges != want_edges:
                edges_ok = False
                log.error("edge table mismatch", version=index)
            for uid in self.rng.sample(sorted(snapshot["Asset"]), 5):
                row = engine.get_object("Asset", uid, at)
                if row is None or any(not _close(row[k], v) for k, v in snapshot["Asset"][uid].items()):
                    objects_ok = False
                    log.error("object mismatch", version=index, uid=uid, got=row)
            pairs = [(r["src"], r["dst"]) for r in snapshot["Feeds"].values()]
            for seed in self.rng.sample(sorted({src for src, _ in pairs}), 3):
                if engine.neighbors(seed, 2, at) != _reachable(pairs, seed, 2):
                    neighbors_ok = False
                    log.error("neighbour set mismatch", version=index, seed=seed)
        self.checks.update(
            node_table=tables_ok, edge_table=edges_ok, point_read=objects_ok, neighbors=neighbors_ok
        )

        position = {version: index for index, version in enumerate(self.versions)}
        history_ok = True
        for uid, truth in fixture.tracked.items():
            raw = engine.property_history("Asset", uid, "load")
            got = sorted((position[version], value) for version, value in raw if version in position)
            if len(got) != len(truth) or any(
                gi != ti or not _close(gv, tv) for (gi, gv), (ti, tv) in zip(got, truth)
            ):
                history_ok = False
                log.error("property history mismatch", uid=uid, got=len(got), want=len(truth))
        self.checks["property_history"] = history_ok

        first, last = fixture.sample_indices[0], fixture.sample_indices[-1]
        a, b = fixture.snapshots[first]["Asset"], fixture.snapshots[last]["Asset"]
        want_diff = sum(1 for uid in a.keys() | b.keys() if a.get(uid) != b.get(uid))
        try:
            got_diff = engine.diff_count(self.versions[first], self.versions[last], "Asset")
            self.checks["diff"] = got_diff == want_diff
            if got_diff != want_diff:
                log.error("diff count mismatch", got=got_diff, want=want_diff)
        except Exception as exc:
            # Kept separate so a failing diff does not discard the checks above.
            self.checks["diff"] = False
            log.error("diff check failed", exc=exc, first=first, last=last)
        log.info("verification finished", checks=self.checks)

    # -- measurements -------------------------------------------------------

    def measure(self) -> None:
        engine, params = self.engine, self.fixture.params
        assets = params.assets

        def point_asof() -> None:
            samples, old, new = [], [], []
            total = len(self.versions)
            for _ in range(200):
                index = self._random_version()
                started = time.perf_counter()
                engine.get_object("Asset", f"a{self.rng.randrange(assets)}", self.versions[index])
                elapsed = _ms(started)
                samples.append(elapsed)
                if index < total / 4:
                    old.append(elapsed)
                elif index >= 3 * total / 4:
                    new.append(elapsed)
            self.ops["point_read_asof"] = _summary(samples)
            if old and new:
                self.ops["point_read_asof"]["oldest_over_newest"] = round(
                    statistics.median(old) / statistics.median(new), 2
                )

        def point_head() -> None:
            samples = []
            for _ in range(100):
                started = time.perf_counter()
                engine.get_object("Asset", f"a{self.rng.randrange(assets)}")
                samples.append(_ms(started))
            self.ops["point_read_head"] = _summary(samples)

        def table(type_name: str, op: str) -> Callable[[], None]:
            def work() -> None:
                samples, rows = [], 0
                for _ in range(6):
                    started = time.perf_counter()
                    rows = engine.node_table(type_name, self.versions[self._random_version()]).num_rows
                    samples.append(_ms(started))
                self.ops[op] = {**_summary(samples), "rows": rows}

            return work

        def history() -> None:
            samples, points = [], 0
            for uid in self.fixture.tracked:
                for _ in range(2):
                    started = time.perf_counter()
                    points = len(engine.property_history("Asset", uid, "load"))
                    samples.append(_ms(started))
            self.ops["property_history"] = {**_summary(samples), "change_points": points}

        def diffs() -> None:
            samples = []
            for _ in range(10):
                index = self.rng.randrange(len(self.versions) - 1)
                started = time.perf_counter()
                engine.diff_count(self.versions[index], self.versions[index + 1], "Asset")
                samples.append(_ms(started))
            self.ops["diff_adjacent"] = _summary(samples)
            samples = []
            for _ in range(2):
                started = time.perf_counter()
                engine.diff_count(self.versions[0], self.versions[-1], "Asset")
                samples.append(_ms(started))
            self.ops["diff_first_to_head"] = _summary(samples)

        def hops(depth: int) -> Callable[[], None]:
            def work() -> None:
                samples, sizes = [], []
                for _ in range(20):
                    at = self.versions[self._random_version()]
                    started = time.perf_counter()
                    found = engine.neighbors(f"a{self.rng.randrange(assets)}", depth, at)
                    samples.append(_ms(started))
                    sizes.append(len(found))
                self.ops[f"neighbors_k{depth}"] = {
                    **_summary(samples),
                    "median_result_size": statistics.median(sizes),
                }

            return work

        def projection() -> None:
            """Traversal without engine support: pull the edge table, traverse in a library."""
            samples = []
            for _ in range(3):
                at = self.versions[self._random_version()]
                started = time.perf_counter()
                edges: pa.Table = engine.node_table("Feeds", at)
                pairs = list(zip(edges.column("src").to_pylist(), edges.column("dst").to_pylist()))
                _reachable(pairs, f"a{self.rng.randrange(assets)}", 3)
                samples.append(_ms(started))
            self.ops["projection_traverse"] = _summary(samples)

        def branches() -> None:
            create, write, delete = [], [], []
            isolated = True
            for round_index in range(5):
                index = self._random_version()
                at = self.versions[index]
                name = f"scenario-{round_index}"
                uid = f"a{self.rng.randrange(assets)}"
                before = engine.get_object("Asset", uid, at)
                changed = {**before, "load": round(before["load"] + 1.0, 3)}
                started = time.perf_counter()
                engine.branch_create(name, at)
                create.append(_ms(started))
                started = time.perf_counter()
                engine.commit(ChangeSet(upserts={"Asset": [changed]}), "what-if", branch=name)
                write.append(_ms(started))
                on_branch = engine.get_object("Asset", uid, branch=name)
                on_live = engine.get_object("Asset", uid, at)
                if not _close(on_branch["load"], changed["load"]) or not _close(
                    on_live["load"], before["load"]
                ):
                    isolated = False
                    log.error("branch isolation violated", branch=name, uid=uid)
                started = time.perf_counter()
                engine.branch_delete(name)
                delete.append(_ms(started))
            self.ops["branch_create"] = _summary(create)
            self.ops["branch_commit"] = _summary(write)
            self.ops["branch_delete"] = _summary(delete)
            self.checks["branch_isolation"] = isolated

        def rejections() -> None:
            self.notes["rejections"] = engine.rejections()

        self._guard("point_read_asof", point_asof)
        self._guard("point_read_head", point_head)
        self._guard("node_table_asof", table("Asset", "node_table_asof"))
        self._guard("edge_table_asof", table("Feeds", "edge_table_asof"))
        self._guard("property_history", history)
        self._guard("diff", diffs)
        for depth in (1, 2, 3):
            self._guard(f"neighbors_k{depth}", hops(depth))
        self._guard("projection_traverse", projection)
        self._guard("branches", branches)
        self._guard("rejections", rejections)
        self._guard("write_during_read", self._write_during_read)

    def _write_during_read(self) -> None:
        """A reader pinned to an old version must be unaffected by concurrent commits."""
        engine, params = self.engine, self.fixture.params
        at = self.versions[len(self.versions) // 2]
        uids = [f"a{self.rng.randrange(params.assets)}" for _ in range(20)]
        baseline = {uid: engine.get_object("Asset", uid, at) for uid in uids}
        reader = engine.clone()
        stop = threading.Event()
        latencies: list[float] = []
        failures: list[str] = []

        def read_loop() -> None:
            try:
                while not stop.is_set():
                    for uid in uids:
                        started = time.perf_counter()
                        row = reader.get_object("Asset", uid, at)
                        latencies.append(_ms(started))
                        if row != baseline[uid]:
                            failures.append(uid)
            except Exception as exc:
                log.error("reader failed during concurrent writes", exc=exc)
                failures.append(f"exception: {exc}")

        thread = threading.Thread(target=read_loop, name="pinned-reader")
        thread.start()
        writes = []
        try:
            for round_index in range(15):
                rows = []
                for uid in self.rng.sample(sorted(self.fixture.state["Asset"]), 20):
                    row = dict(self.fixture.state["Asset"][uid])
                    row["load"] = round(self.rng.uniform(0, row["capacity"]), 3)
                    self.fixture.state["Asset"][uid] = row
                    rows.append(row)
                started = time.perf_counter()
                engine.commit(ChangeSet(upserts={"Asset": rows}), f"concurrent-{round_index}")
                writes.append(_ms(started))
        finally:
            stop.set()
            thread.join(timeout=120)
            reader.close()
        self.ops["read_during_write"] = _summary(latencies)
        self.ops["write_during_read"] = _summary(writes)
        self.checks["pinned_reader_stable"] = not failures and bool(latencies)
        if failures:
            log.error("pinned reader saw changes", count=len(failures), sample=failures[:3])

    # -- verdict ------------------------------------------------------------

    def gates(self) -> dict[str, dict]:
        def p95(op: str) -> float | None:
            return self.ops.get(op, {}).get("p95_ms")

        def under(op: str, limit: float) -> dict:
            value = p95(op)
            return {"value": value, "limit": limit, "pass": value is not None and value <= limit}

        ratio = self.ops.get("point_read_asof", {}).get("oldest_over_newest")
        native = under("neighbors_k2", GATES["neighbors_k2_ms"])
        projected = under("projection_traverse", GATES["projection_traverse_ms"])
        refused = self.notes.get("rejections") or {}
        return {
            "correctness": {"value": self.checks, "pass": bool(self.checks) and all(self.checks.values())},
            "point_read_asof": under("point_read_asof", GATES["point_read_asof_ms"]),
            "history_scaling": {
                "value": ratio,
                "limit": GATES["history_scaling_ratio"],
                "pass": ratio is not None and ratio <= GATES["history_scaling_ratio"],
            },
            "node_table_asof": under("node_table_asof", GATES["node_table_asof_ms"]),
            "property_history": under("property_history", GATES["property_history_ms"]),
            "traversal": {
                "native": native,
                "projection": projected,
                "pass": native["pass"] or projected["pass"],
            },
            "branch_create": under("branch_create", GATES["branch_create_ms"]),
            "native_constraints": {
                "value": refused,
                "pass": all(refused.get(case) for case in NATIVE_REJECTIONS),
            },
            "commit_soft": under("commit", SOFT_COMMIT_MS),
        }


def run(engine_name: str, fixture_name: str, config: Config, ontology: Ontology, out: Path) -> dict:
    """Benchmark one engine on one fixture and write the result as JSON."""
    log.info("benchmark started", engine=engine_name, fixture=fixture_name)
    started = time.perf_counter()
    engine = ENGINES[engine_name](config, ontology)
    bench = Bench(engine, Fixture(FIXTURES[fixture_name]))
    try:
        bench.load()
        bench._guard("verify", bench.verify)
        bench.measure()
    finally:
        engine.close()
    gates = bench.gates()
    result = {
        "run_id": RUN_ID,
        "engine": engine_name,
        "fixture": asdict(bench.fixture.params),
        "versions": len(bench.versions),
        "notes": bench.notes,
        "checks": bench.checks,
        "ops": bench.ops,
        "gates": gates,
        "hard_gates_passed": all(g["pass"] for name, g in gates.items() if name != "commit_soft"),
        "elapsed_s": round(time.perf_counter() - started, 1),
    }
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{engine_name}-{fixture_name}.json"
    path.write_text(json.dumps(result, indent=2, default=str))
    log.info(
        "benchmark finished",
        engine=engine_name,
        fixture=fixture_name,
        hard_gates_passed=result["hard_gates_passed"],
        result_file=str(path),
        elapsed_s=result["elapsed_s"],
    )
    return result
