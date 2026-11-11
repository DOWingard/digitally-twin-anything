"""A separate process that uses the store, started by the tests.

    commit <database> <worker> <count>   commit <count> zones, print their positions as JSON
    die <database> <operation> <step>    run commit, migrate or install and exit hard at <step>

Exit code 77 means the process stopped itself at the requested step.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys

from dta_core.store import Store, StoreSettings
from dta_store_testkit import at, changes, meta, water_ontology, water_ontology_v2

DIED = 77


def _settings(database: str) -> StoreSettings:
    return dataclasses.replace(StoreSettings.from_env(), database=database, lock_timeout_s=60.0)


def commit(database: str, worker: str, count: int) -> None:
    store = Store(_settings(database), water_ontology())
    positions = []
    for index in range(count):
        version = store.commit(
            changes({"Zone": [{"uid": f"zone:{worker}-{index}", "name": f"{worker} {index}"}]}),
            meta(t=at(5), run=worker),
        )
        positions.append(version.seq)
    store.close()
    print(json.dumps(positions))


def die(database: str, operation: str, step: str) -> None:
    store = Store(_settings(database), water_ontology())

    def stop(reached: str) -> None:
        if reached == step:
            # No cleanup of any kind: the connection is cut as by a crash.
            os._exit(DIED)

    store._fault = stop
    if operation == "commit":
        store.commit(
            changes(
                {
                    "Zone": [{"uid": "zone:doomed", "name": "written by a process that died"}],
                    "Tank": [{"uid": "node:doomed", "name": "doomed", "capacity": 1.0}],
                }
            ),
            meta(t=at(5)),
        )
    elif operation == "migrate":
        store.migrate(water_ontology_v2(), meta("migration", at(5)))
    elif operation == "install":
        store.install("twin-under-test")
    else:
        raise SystemExit(f"unknown operation {operation}")
    print("finished")


if __name__ == "__main__":
    if sys.argv[1] == "commit":
        commit(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    elif sys.argv[1] == "die":
        die(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        raise SystemExit(f"unknown mode {sys.argv[1]}")
