"""How long a frame takes on a history of full size: 200 tanks whose levels change at
every one of 2,000 versions, 50 junctions whose demands do, 400 pipes between them.
Run with `-m scale`."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pytest
from dta_core.log import Log
from dta_core.store import LIVE_BRANCH, VersionRef
from dta_runtime.protocol import FrameRef
from dta_runtime.training import frame
from dta_runtime.twin import Twin
from training_testkit import STEP, at, commit, junction, link, node, pipe, tank

pytestmark = pytest.mark.scale
log = Log("test.scale")

SERIES = 200
JUNCTIONS = 50
VERSIONS = 2_000
FEATURES = [
    {"property": "Tank.level", "lag": STEP},
    "Tank.max_level",
    {"property": "Junction.demand", "via": "Pipe", "agg": "mean"},
]
THRESHOLDS_S = {
    "frame 200 series x 2,000 versions, 2 own features + 1 neighbour aggregate": 6.0,
    "the same, uids of 3 series": 1.5,
}


def _tank(index: int) -> str:
    return f"T{index:03d}"


def _junction(index: int) -> str:
    return f"J{index:02d}"


def _level(index: int, version: int) -> float:
    return round(1.0 + (index * 7 + version * 3) % 170 / 10.0, 1)


def _demand(index: int, version: int) -> float:
    return float((index * 11 + version * 5) % 97)


@dataclass
class Network:
    twin: Twin
    first: VersionRef
    last: VersionRef
    load_s: float
    measured: dict[str, float] = field(default_factory=dict)


@pytest.fixture(scope="module")
def network(module_twin: Callable[[], Twin]) -> Iterator[Network]:
    twin = module_twin()
    started = time.perf_counter()
    first = commit(
        twin,
        at(1),
        {
            "Tank": [tank(_tank(index), _level(index, 0)) for index in range(SERIES)],
            "Junction": [
                junction(_junction(index), _demand(index, 0)) for index in range(JUNCTIONS)
            ],
            "Pipe": [
                found
                for index in range(SERIES)
                for found in (
                    pipe(f"O{index:03d}", _tank(index), _junction(index % JUNCTIONS)),
                    pipe(f"I{index:03d}", _junction(index * 7 % JUNCTIONS), _tank(index)),
                )
            ],
        },
    )
    last = first
    for version in range(1, VERSIONS):
        upserts = {
            "Tank": [
                {"uid": node(_tank(index)), "level": _level(index, version)}
                for index in range(SERIES)
            ],
            "Junction": [
                {"uid": node(_junction(index)), "demand": _demand(index, version)}
                for index in range(JUNCTIONS)
            ],
        }
        if version % 100 == 0:
            # A pipe leaves its tank for another junction.
            moved = version // 100
            upserts["Pipe"] = [
                {"uid": link(f"O{moved:03d}"), "end_node": node(_junction((moved + 9) % JUNCTIONS))}
            ]
        last = commit(twin, at(1 + version), upserts)
        if version % 250 == 0:
            log.info("scale fixture: versions written", versions=version)
    loaded = Network(twin, first, last, time.perf_counter() - started)
    yield loaded
    report = [f"scale fixture loaded in {loaded.load_s:.1f} s"]
    report += [
        f"{label}: {loaded.measured[label]:.2f} s (threshold {limit:.1f} s)"
        for label, limit in THRESHOLDS_S.items()
        if label in loaded.measured
    ]
    one, five, fifteen = os.getloadavg()
    report.append(f"load average (1, 5, 15 min): {one:.2f} {five:.2f} {fifteen:.2f}")
    print("\n" + "\n".join(report))
    log.info("scale measurements", **{k: round(v, 2) for k, v in loaded.measured.items()})


def _ref(network: Network, **more: object) -> FrameRef:
    return FrameRef(
        target="Tank.level",
        branch=LIVE_BRANCH,
        start=network.first.id,
        end=network.last.id,
        step=STEP,
        features=FEATURES,
        **more,
    )


def _timed(network: Network, label: str, ref: FrameRef):
    samples = []
    for _ in range(3):
        started = time.perf_counter()
        table = frame(network.twin.store, network.twin.ontology, ref)
        samples.append(time.perf_counter() - started)
    network.measured[label] = max(samples)
    return table


def test_a_frame_of_every_series(network: Network) -> None:
    label = "frame 200 series x 2,000 versions, 2 own features + 1 neighbour aggregate"
    table = _timed(network, label, _ref(network))
    assert table.num_rows == SERIES * VERSIONS
    assert table.schema.names == ["unique_id", "ds", "y", "Tank_level_lag1", "Tank_max_level",
                                  "Junction_demand_mean"]  # fmt: skip
    rows = table.slice(7 * VERSIONS, VERSIONS).to_pylist()
    assert {row["unique_id"] for row in rows} == {node(_tank(7))}
    for version in (0, 1, 2, 999, 1999):
        row = rows[version]
        assert row["ds"] == at(1 + version)
        assert row["y"] == _level(7, version)
        assert row["Tank_level_lag1"] == (None if version == 0 else _level(7, version - 1))
        # The pipe out of tank 7 moves from junction 7 to junction 16 at version 700.
        out = 16 if version >= 700 else 7
        assert row["Junction_demand_mean"] == pytest.approx(
            sum(_demand(found, version) for found in {out, 49}) / 2
        )
    assert network.measured[label] <= THRESHOLDS_S[label], f"{network.measured[label]:.2f} s"


def test_a_frame_of_three_series(network: Network) -> None:
    label = "the same, uids of 3 series"
    uids = [node(_tank(index)) for index in (3, 77, 150)]
    table = _timed(network, label, _ref(network, uids=uids))
    assert table.num_rows == 3 * VERSIONS
    assert sorted(set(table.column("unique_id").to_pylist())) == uids
    assert network.measured[label] <= THRESHOLDS_S[label], f"{network.measured[label]:.2f} s"
