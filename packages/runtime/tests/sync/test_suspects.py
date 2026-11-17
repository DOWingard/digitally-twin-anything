"""A run leaves the readings of suspect signals out of the graph and keeps them on their
signal records, in the version it writes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from dta_core.schema import FactKind
from dta_core.store import ChangeSet, CommitMeta, Provenance
from dta_runtime import fidelity
from dta_runtime.clock import FixedClock
from dta_runtime.twin import Twin
from live_testkit import LIVE, Folder, at, levels_source, registry_source
from sync_testkit import changes_of, objects

SIGNAL = fidelity.signal_uid("Tank.level", "node:T1")


def flag(twin: Twin) -> None:
    """Mark the level of tank T1 suspect, as a fidelity check would."""
    head = twin.store.head()
    row = {
        "uid": SIGNAL,
        "property": "Tank.level",
        "object": "node:T1",
        "status": "suspect",
        "reason": "stuck",
        "since": head.t,
        "clear_checks": 0,
        "detail": "{}",
    }
    twin.store.commit(
        ChangeSet(
            upserts={"TwinSignal": [row]},
            provenance=Provenance(source="fidelity", kind=FactKind.inferred, t_valid=head.t),
        ),
        CommitMeta(cause="derived", actor="fidelity", t=head.t, note="a signal flagged"),
    )


def test_a_suspect_signals_reading_goes_to_its_record_not_the_graph(
    sync_twin: Callable[..., Twin], tick: Callable[..., Any], tmp_path: Path
) -> None:
    registry, scada = Folder(tmp_path / "registry"), Folder(tmp_path / "scada")
    sources = {
        "registry": registry_source(registry),
        "scada": levels_source(scada, [("T1", at(5), 1.0), ("T1", at(10), 1.2)]),
    }
    twin = sync_twin(sources, sync=LIVE)
    clock = FixedClock(at(16))
    assert tick(twin, clock).status == "succeeded"
    flag(twin)
    assert fidelity.suspects(twin) == {("Tank.level", "node:T1")}

    scada.append("levels", [("T1", at(20), 1.7), ("T2", at(21), 3.0), ("T1", at(31), 2.0)])
    clock.set(at(31))
    report = tick(twin, clock)

    assert report.status == "succeeded"
    (version,) = twin.store.versions(run=report.run)
    tanks = objects(twin.store, "Tank")
    # T1's reading is held back; T2's, which is not suspect, is written.
    assert (tanks["node:T1"]["level"], tanks["node:T2"]["level"]) == (1.2, 3.0)
    assert changes_of(twin.store, "node:T1", "level") == [(at(15), 1.2)]
    held = fidelity.signal(twin, SIGNAL)
    assert held is not None
    assert (held["reading"], held["reading_at"], held["status"]) == (1.7, at(30), "suspect")
    # In the version the run wrote, not one of its own.
    changed = twin.store.history(SIGNAL, properties=["reading"]).to_pylist()
    assert changed[-1]["_version"] == version.id
