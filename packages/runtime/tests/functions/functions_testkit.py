"""Twins with known histories for the tests of the functions, and plain answers to
the questions the built-ins answer, worked out by reading the store version by version.

The story, on the water network's ontology (times are days of January 2026 and hours):

    version   time      what changed
    #2        5, 06:00  the network: tanks T1 5.0, T2 1.5, T3 8.0, T5 without a level
    #3        5, 12:00  T1 1.0, T2 3.0
    #4        5, 18:00  T4 appears at 0.5; T3 1.8
    #5        5, 18:00  T4 6.0 (the same time as #4)
    #6        6, 06:00  T1 loses its level; T3 is removed
    #7        6, 12:00  T1 1.2, T2 1.0
    #8        7, 00:00  T2 1.9, T4 1.0
    #9        7, 12:00  T1 7.0

    P1: J1 -> J2 (open, 10 m)    P2: J2 -> J3 (closed, 10 m)   P3: J1 -> T1 (open, 5 m)
    P4: T1 -> J3 (open, 5 m)     P5: J1 -> J3 (closed, 100 m)  P6: R1 -> J1 (no status, 30 m)
    V1: J2 -> T2 (open valve)    sensors S1, S2 on J1 and S3 on T1
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from api_testkit import manifest_for
from dta_core.errors import DtaError
from dta_core.manifest.models import FunctionSpec
from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance, StoreSettings, VersionRef
from dta_runtime.artifacts import ArtifactStore
from dta_runtime.functions import Context, resolve, run
from dta_runtime.graph import GraphCache
from dta_runtime.protocol import Answer
from dta_runtime.twin import Bundle, Twin, install_twin

T0 = datetime(2026, 1, 5, tzinfo=UTC)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
FIRST, LAST = 2, 9
# The id the tests' functions go by; refusals name it.
FUNCTION = "asked"

State = dict[str, float | None]
Row = dict[str, Any]


def at(day: int, hour: int = 0, minute: int = 0) -> datetime:
    """A time of January 2026."""
    return datetime(2026, 1, day, hour, minute, tzinfo=UTC)


def node(key: str) -> str:
    return f"node:{key}"


def link(key: str) -> str:
    return f"link:{key}"


def tank(key: str, level: float | None, elevation: float = 60.0) -> Row:
    return {
        "uid": node(key),
        "name": key,
        "elevation": elevation,
        "min_level": 0.0,
        "max_level": 20.0,
        "level": level,
        "volume_curve": [0.0, 10.0],
        "x": 0.0,
        "y": 0.0,
    }


def levels(*pairs: tuple[str, float | None]) -> list[Row]:
    return [{"uid": node(key), "level": level} for key, level in pairs]


def commit(
    twin: Twin,
    t: datetime,
    upserts: Mapping[str, list[Row]] | None = None,
    deletes: Mapping[str, list[str]] | None = None,
) -> VersionRef:
    changes = ChangeSet(
        upserts=dict(upserts or {}),
        deletes=dict(deletes or {}),
        provenance=Provenance(source="scada", kind=FactKind.observed, t_valid=t),
    )
    return twin.store.commit(changes, CommitMeta(cause="sync", actor="scada", t=t))


@dataclass
class History:
    """A twin and the versions written to it, by position on the branch."""

    twin: Twin
    versions: dict[int, VersionRef] = field(default_factory=dict)
    graphs: GraphCache = field(init=False)

    def __post_init__(self) -> None:
        self.graphs = GraphCache(self.twin.store)

    @property
    def head(self) -> VersionRef:
        return self.versions[max(self.versions)]

    def context(
        self,
        version: VersionRef | int | None = None,
        *,
        budget_s: float = 120.0,
        forecast: Callable[..., Any] | None = None,
    ) -> Context:
        """What a run at a version is given; the head when none is named."""
        if version is None:
            pinned = self.head
        elif isinstance(version, int):
            pinned = self.versions[version]
        else:
            pinned = version
        return Context(
            store=self.twin.store,
            ontology=self.twin.store.ontology_at(pinned),
            version=pinned,
            graphs=self.graphs,
            deadline=time.monotonic() + budget_s,
            forecast=forecast,
        )


def _install(settings: StoreSettings, ontology_text: str) -> History:
    installed = install_twin(
        settings, Bundle(manifest=manifest_for(ontology_text), ontology_text=ontology_text), t=T0
    )
    history = History(twin=installed.twin)
    history.versions[installed.version.seq] = installed.version
    return history


def _pipe(key: str, start: str, end: str, status: str | None, length: float) -> Row:
    return {
        "uid": link(key),
        "start_node": node(start),
        "end_node": node(end),
        "status": status,
        "length": length,
        "diameter": 100.0,
    }


def build_story(settings: StoreSettings, ontology_text: str) -> History:
    """The story of the module's docstring, written to a new database."""
    history = _install(settings, ontology_text)
    twin = history.twin
    network: dict[str, list[Row]] = {
        "Reservoir": [{"uid": node("R1"), "name": "R1", "elevation": 100.0, "head": 120.0}],
        "Junction": [
            {"uid": node("J1"), "name": "J1", "elevation": 50.0, "demand": 2.0},
            {"uid": node("J2"), "name": "J2", "elevation": 45.0, "demand": 4.0},
            {"uid": node("J3"), "name": "J3", "elevation": 40.0},
        ],
        "Tank": [
            tank("T1", 5.0, 60.0),
            tank("T2", 1.5, 61.0),
            tank("T3", 8.0, 62.0),
            tank("T5", None, 65.0),
        ],
        "Pipe": [
            _pipe("P1", "J1", "J2", "open", 10.0),
            _pipe("P2", "J2", "J3", "closed", 10.0),
            _pipe("P3", "J1", "T1", "open", 5.0),
            _pipe("P4", "T1", "J3", "open", 5.0),
            _pipe("P5", "J1", "J3", "closed", 100.0),
            _pipe("P6", "R1", "J1", None, 30.0),
        ],
        "Valve": [
            {
                "uid": link("V1"),
                "start_node": node("J2"),
                "end_node": node("T2"),
                "valve_type": "prv",
                "status": "open",
            }
        ],
        "Sensor": [
            {
                "uid": "sensor:S1",
                "measures": node("J1"),
                "quantity": "pressure",
                "active": True,
                "sample_interval": 60,
            },
            {
                "uid": "sensor:S2",
                "measures": node("J1"),
                "quantity": "flow",
                "active": True,
                "sample_interval": 30,
            },
            {"uid": "sensor:S3", "measures": node("T1"), "quantity": "level", "active": False},
        ],
    }
    steps: list[tuple[datetime, dict[str, list[Row]], dict[str, list[str]]]] = [
        (at(5, 6), network, {}),
        (at(5, 12), {"Tank": levels(("T1", 1.0), ("T2", 3.0))}, {}),
        (at(5, 18), {"Tank": [tank("T4", 0.5, 64.0), *levels(("T3", 1.8))]}, {}),
        (at(5, 18), {"Tank": levels(("T4", 6.0))}, {}),
        (at(6, 6), {"Tank": levels(("T1", None))}, {"Tank": [node("T3")]}),
        (at(6, 12), {"Tank": levels(("T1", 1.2), ("T2", 1.0))}, {}),
        (at(7, 0), {"Tank": levels(("T2", 1.9), ("T4", 1.0))}, {}),
        (at(7, 12), {"Tank": levels(("T1", 7.0))}, {}),
    ]
    for seq, (t, upserts, deletes) in enumerate(steps, start=FIRST):
        version = commit(twin, t, upserts, deletes)
        assert version.seq == seq, (version.seq, seq)
        history.versions[seq] = version
    return history


GENERATED_TANKS = [f"node:G{index:02d}" for index in range(1, 33)]
GENERATED_VERSIONS = 60
GENERATED_STEP = timedelta(minutes=15)


def build_generated(settings: StoreSettings, ontology_text: str) -> History:
    """Tanks whose levels change, become null, disappear and come back, over 60 versions
    of which one in five has the time of the one before it."""
    history = _install(settings, ontology_text)
    twin = history.twin
    rng = random.Random(20261116)
    t = T0 + GENERATED_STEP
    first = commit(
        twin,
        t,
        {"Tank": [tank(uid.split(":")[1], round(rng.uniform(1, 9), 2)) for uid in GENERATED_TANKS]},
    )
    history.versions[first.seq] = first
    present = set(GENERATED_TANKS)
    for index in range(GENERATED_VERSIONS - 1):
        if index % 5 != 4:
            t += GENERATED_STEP
        upserts: list[Row] = []
        deletes: list[str] = []
        for uid in GENERATED_TANKS:
            draw = rng.random()
            if uid in present and draw < 0.03 and len(present) > 8:
                deletes.append(uid)
            elif uid not in present and draw < 0.25:
                upserts.append(tank(uid.split(":")[1], round(rng.uniform(1, 9), 2)))
            elif uid in present and draw < 0.3:
                level = None if rng.random() < 0.15 else round(rng.uniform(1, 9), 2)
                upserts.append({"uid": uid, "level": level})
        if not upserts and not deletes:
            upserts.append({"uid": sorted(present)[0], "level": round(rng.uniform(1, 9), 2)})
        present |= {row["uid"] for row in upserts}
        present -= set(deletes)
        version = commit(
            twin,
            t,
            {"Tank": upserts} if upserts else None,
            {"Tank": deletes} if deletes else None,
        )
        history.versions[version.seq] = version
    return history


def ask(
    history: History,
    entry: str,
    call: Mapping[str, Any],
    *,
    fixed: Mapping[str, Any] | None = None,
    version: VersionRef | int | None = None,
) -> Answer:
    """The answer of a built-in the way the API gets it: the manifest's fixed parameters
    and the call's resolved, then run at a version (the head when none is named)."""
    spec = FunctionSpec(kind="builtin", entry=entry, params=dict(fixed or {}))
    return run(FUNCTION, entry, resolve(FUNCTION, spec, call), history.context(version))


def refusal(history: History, entry: str, call: Mapping[str, Any], **more: Any) -> DtaError:
    with pytest.raises(DtaError) as raised:
        ask(history, entry, call, **more)
    return raised.value


def rows(answer: Answer) -> list[Row]:
    assert isinstance(answer.value, pa.Table)
    return answer.value.to_pylist()


def same(actual: Sequence[Row], expected: Sequence[Row]) -> None:
    """The rows are the expected ones, in order; numbers that are not whole to within
    what adding them in another order can change."""
    assert len(actual) == len(expected), (len(actual), len(expected))
    for index, (found, wanted) in enumerate(zip(actual, expected, strict=True)):
        assert list(found) == list(wanted), index
        for name, value in wanted.items():
            if isinstance(value, float):
                assert found[name] == pytest.approx(value, rel=1e-9, abs=1e-9), (index, name)
            else:
                assert found[name] == value, (index, name, found, wanted)


# -- the same answers, worked out version by version ----------------------------


def states(history: History, first: int, last: int) -> list[tuple[VersionRef, State]]:
    """The level of every tank at each version from `first` to `last`: one read each."""
    twin = history.twin
    found = []
    for seq in range(first, last + 1):
        version = history.versions[seq]
        table = twin.store.table("Tank", columns=["level"], at=version, ontology=twin.ontology)
        found.append(
            (
                version,
                dict(
                    zip(
                        table.column("uid").to_pylist(),
                        table.column("level").to_pylist(),
                        strict=True,
                    )
                ),
            )
        )
    return found


def in_force(walked: Sequence[tuple[VersionRef, State]]) -> list[tuple[datetime, State]]:
    """What is in force at each time: of versions with one time, the latest."""
    found: list[tuple[datetime, State]] = []
    for version, state in walked:
        if found and found[-1][0] == version.t:
            found[-1] = (version.t, state)
        else:
            found.append((version.t, state))
    return found


def _uids(walked: Sequence[tuple[VersionRef, State]], only: Sequence[str] | None) -> list[str]:
    seen = {uid for _, state in walked for uid in state}
    return sorted(seen if only is None else seen & set(only))


def reference_spells(
    walked: Sequence[tuple[VersionRef, State]],
    holds: Callable[[float], bool],
    extreme: Callable[[float, float], float],
    uids: Sequence[str] | None = None,
) -> list[Row]:
    timeline = in_force(walked)
    rows: list[Row] = []
    for uid in _uids(walked, uids):
        spell: Row | None = None
        for t, state in timeline:
            value = state.get(uid)
            if value is not None and holds(value):
                if spell is None:
                    spell = {"uid": uid, "start": t, "end": None, "extreme": value}
                else:
                    spell["extreme"] = extreme(spell["extreme"], value)
            elif spell is not None:
                spell["end"] = t
                rows.append(spell)
                spell = None
        if spell is not None:
            rows.append(spell)
    return rows


def reference_integral(
    walked: Sequence[tuple[VersionRef, State]],
    period: timedelta | None = None,
    uids: Sequence[str] | None = None,
) -> list[Row]:
    timeline = in_force(walked)
    start, end = walked[0][0].t, walked[-1][0].t

    def number(moment: datetime) -> int:
        return 0 if period is None else (moment - EPOCH) // period

    first, last = number(start), number(end)
    if period is not None and end > start and EPOCH + last * period == end:
        last -= 1
    rows: list[Row] = []
    for uid in _uids(walked, uids):
        sums = {k: [0.0, 0.0] for k in range(first, last + 1)}
        for index, (a, state) in enumerate(timeline):
            b = timeline[index + 1][0] if index + 1 < len(timeline) else end
            value = state.get(uid)
            if value is None:
                continue
            for k in range(number(a), number(b) + 1):
                low = a if period is None else max(a, EPOCH + k * period)
                high = b if period is None else min(b, EPOCH + (k + 1) * period)
                seconds = (high - low).total_seconds()
                if seconds > 0:
                    sums[k][0] += value * seconds
                    sums[k][1] += seconds
        for k, (value, covered) in sums.items():
            row: Row = {"uid": uid}
            if period is not None:
                row["period_start"] = EPOCH + k * period
            rows.append({**row, "value": value, "covered_s": covered})
    return rows


def reference_series(
    walked: Sequence[tuple[VersionRef, State]], uids: Sequence[str] | None = None
) -> list[Row]:
    """The level at the first version, then a row per version at which what is in force
    for a tank is not what it was; a tank that is not there has no level."""
    keyed: list[tuple[str, int, Row]] = []
    wanted = None if uids is None else set(uids)
    previous: State = {}
    for index, (version, state) in enumerate(walked):
        for uid in sorted(set(previous) | set(state)):
            if wanted is not None and uid not in wanted:
                continue
            value = state.get(uid)
            initial = index == 0 and uid in state
            if initial or (index > 0 and value != previous.get(uid)):
                keyed.append((uid, version.seq, {"uid": uid, "t": version.t, "value": value}))
        previous = state
    return [row for _, _, row in sorted(keyed, key=lambda item: (item[0], item[1]))]


def reference_grid(
    walked: Sequence[tuple[VersionRef, State]], step: timedelta, uids: Sequence[str] | None = None
) -> list[Row]:
    """The level in force at each time of the grid, for the tanks that are there then."""
    start, end = walked[0][0].t, walked[-1][0].t
    wanted = None if uids is None else set(uids)
    rows: list[Row] = []
    moment = start
    while moment <= end:
        _, state = max(
            ((version, state) for version, state in walked if version.t <= moment),
            key=lambda item: item[0].seq,
        )
        rows.extend(
            {"uid": uid, "t": moment, "value": value}
            for uid, value in state.items()
            if wanted is None or uid in wanted
        )
        moment += step
    return sorted(rows, key=lambda row: (row["uid"], row["t"]))


# -- what code of the twin's own is handed ---------------------------------------


class StoreReader:
    """A reader of the twin at one version, over the store itself: the paths the tests'
    functions read are those of the API's object listings."""

    def __init__(self, twin: Twin, version: VersionRef) -> None:
        self._twin = twin
        self._version = version
        self.at = version.id
        self.branch = version.branch

    def _columns(self, params: Mapping[str, Any]) -> list[str] | None:
        columns = params.get("columns")
        return columns.split(",") if isinstance(columns, str) else columns

    def table(self, path: str, **params: Any) -> pa.Table:
        kind, _, type_name = path.strip("/").partition("/")
        if kind != "objects" or not type_name or "/" in type_name:
            raise ValueError(f"this reader serves /objects/<Type>, not {path!r}")
        return self._twin.store.table(
            type_name,
            columns=self._columns(params),
            at=self._version,
            ontology=self._twin.ontology,
            order_by=[("uid", "asc")],
        )

    def get(self, path: str, **params: Any) -> Any:
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "objects":
            raise ValueError(f"this reader serves /objects/<Type>/<uid>, not {path!r}")
        found = self._twin.store.get(parts[2], at=self._version, ontology=self._twin.ontology)
        if found is None or found.type != parts[1]:
            raise LookupError(f"no {parts[1]} {parts[2]!r} at {self.at}")
        return {"uid": found.uid, "type": found.type, "properties": dict(found.values)}

    def frame(self, ref: Any) -> pa.Table:
        raise NotImplementedError("the functions under test read no frame")


class KeptArtifacts:
    """The twin's artifact files in a folder of the test."""

    def __init__(self, root: Path) -> None:
        self._store = ArtifactStore(root)
        self.media_types: dict[str, str] = {}

    def get(self, sha256: str) -> bytes:
        return self._store.read(sha256)

    def put(self, content: bytes, media_type: str) -> str:
        sha256 = self._store.put(content).sha256
        self.media_types[sha256] = media_type
        return sha256
