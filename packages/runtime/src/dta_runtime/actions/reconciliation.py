"""Whether an executed action had its effect.

Once its effective time plus its settle time has passed, an executed action that set a
control is compared with what the twin observes: the value of the control's target on
the object, in force at the clock's `now`, against the value the action set (equal, or
within the action type's tolerance). A value the twin did not observe (inferred,
assumed) says nothing about the real system, and the record waits. Every record that is
settled is written in one version, and a record is reconciled once.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from dta_core.log import Log
from dta_core.schema import FactKind
from dta_core.store import Selector, UnknownVersion, VersionRef

from ..twin import Twin
from . import catalog
from .checks import is_number, same
from .records import (
    EXECUTED,
    MISMATCH,
    OK,
    PENDING,
    RECORD_TYPE,
    canonical,
    record_document,
    write,
)

log = Log("runtime.actions")

_LOCK = threading.Lock()
_PROVENANCE = "_prov"


@dataclass(frozen=True)
class Reconciled:
    """One executed action compared with what was observed afterwards.

    `value` is what the action set, `observed` the value in force at `reconciled_at`,
    `observed_at` and `observed_source` that value's valid time and source.
    """

    record: str
    action: str
    control: str
    object: str
    target: str
    value: Any
    tolerance: float
    outcome: str
    observed: Any
    observed_at: datetime | None
    observed_source: str | None
    effective_at: datetime
    reconciled_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def version_at(twin: Twin, moment: datetime) -> VersionRef | None:
    """The last version of the live line whose time is not later than `moment`."""
    try:
        return twin.store.resolve(Selector("time", time=moment))[0]
    except UnknownVersion:
        return None


def matches(observed: Any, expected: Any, tolerance: float) -> bool:
    if is_number(observed) and is_number(expected):
        gap = abs(float(observed) - float(expected))
        return gap <= tolerance or math.isclose(
            float(observed), float(expected), rel_tol=1e-9, abs_tol=1e-12
        )
    return same(observed, expected)


def _effective(record: dict[str, Any]) -> datetime | None:
    if record.get("effective_at") is not None:
        return record["effective_at"]
    found = (record.get("execution") or {}).get("at")
    return datetime.fromisoformat(found) if isinstance(found, str) else None


def _observed(
    twin: Twin, version: VersionRef, target: str, uids: list[str]
) -> dict[str, tuple[Any, dict[str, Any]]]:
    """The value of `target` on each object, with its provenance, at a version: one read
    per concrete type the target's type covers."""
    ontology = twin.ontology
    type_name, _, name = target.partition(".")
    try:
        concrete = ontology.concrete(type_name)
    except KeyError:
        log.error("an action's target names a type the ontology does not have", target=target)
        return {}
    found: dict[str, tuple[Any, dict[str, Any]]] = {}
    for member in concrete:
        if not ontology.type(member).has_field(name):
            continue
        table = twin.store.table(
            member,
            columns=[name],
            filter=[("uid", "in", uids)],
            at=version,
            prov=True,
            ontology=ontology,
        )
        for row in table.to_pylist():
            provenance = json.loads(row[_PROVENANCE] or "{}")
            found[row["uid"]] = (row[name], provenance.get(name) or provenance.get("*") or {})
    return found


def reconcile(twin: Twin, *, now: datetime) -> list[Reconciled]:
    """Compare every executed record that is due with what is observed in force at
    `now`, write the outcomes as one version, and return them. A record whose target
    has no observed value in force stays pending."""
    started = time.perf_counter()
    with _LOCK:
        store = twin.store
        head = store.head()
        if not twin.ontology.has_type(RECORD_TYPE):
            return []
        table = store.table(
            RECORD_TYPE,
            filter=[("status", "=", EXECUTED), ("reconciliation", "=", PENDING)],
            at=head,
            ontology=twin.ontology,
        )
        pending = [record_document(row["uid"], row) for row in table.to_pylist()]
        if not pending:
            log.debug("no executed action awaits reconciliation", now=now)
            return []
        actions = {found["id"]: found for found in catalog.types(twin, at=head)}
        due: list[tuple[dict[str, Any], dict[str, Any], datetime]] = []
        waiting = 0
        for record in pending:
            action = actions.get(record["action"])
            if action is None or not action.get("control") or not action.get("target"):
                log.warn(
                    "an executed record awaits reconciliation but its action sets no control",
                    record=record["uid"],
                    action=record["action"],
                )
                continue
            effective = _effective(record)
            if effective is None:
                log.warn(
                    "an executed record has no effective time; it cannot be reconciled",
                    record=record["uid"],
                )
                continue
            settle = catalog.settle_of(action) or timedelta(0)
            if now < effective + settle:
                waiting += 1
                continue
            due.append((record, action, effective))
        if not due:
            log.debug("no executed action is due for reconciliation", pending=waiting, now=now)
            return []
        version = version_at(twin, now)
        if version is None:
            log.warn("the live line has no version at or before now; nothing reconciled", now=now)
            return []

        by_target: dict[str, set[str]] = defaultdict(set)
        for record, action, _ in due:
            by_target[action["target"]].add(record["object"])
        observed = {
            target: _observed(twin, version, target, sorted(uids))
            for target, uids in by_target.items()
        }

        settled: list[Reconciled] = []
        rows: list[dict[str, Any]] = []
        unobserved = 0
        for record, action, effective in due:
            value, provenance = observed[action["target"]].get(record["object"], (None, {}))
            if value is None or provenance.get("k") != FactKind.observed.value:
                unobserved += 1
                log.debug(
                    "no observed value is in force for an executed action; it stays pending",
                    record=record["uid"],
                    target=action["target"],
                    kind=provenance.get("k"),
                )
                continue
            expected = (record.get("params") or {}).get("value")
            tolerance = float(action.get("tolerance") or 0.0)
            outcome = OK if matches(value, expected, tolerance) else MISMATCH
            gap = (
                abs(float(value) - float(expected))
                if is_number(value) and is_number(expected)
                else None
            )
            moment = provenance.get("t")
            observed_at = datetime.fromisoformat(moment) if isinstance(moment, str) else None
            rows.append(
                {
                    "uid": record["uid"],
                    "reconciliation": outcome,
                    "observed": canonical(
                        {
                            "value": value,
                            "expected": expected,
                            "difference": gap,
                            "tolerance": tolerance,
                            "t": observed_at,
                            "source": provenance.get("s"),
                            "kind": provenance.get("k"),
                            "version": version.id,
                        }
                    ),
                    "reconciled_at": now,
                }
            )
            settled.append(
                Reconciled(
                    record=record["uid"],
                    action=record["action"],
                    control=action["control"],
                    object=record["object"],
                    target=action["target"],
                    value=expected,
                    tolerance=tolerance,
                    outcome=outcome,
                    observed=value,
                    observed_at=observed_at,
                    observed_source=provenance.get("s"),
                    effective_at=effective,
                    reconciled_at=now,
                )
            )
        if rows:
            write(
                twin,
                {RECORD_TYPE: rows},
                now=now,
                note=f"{len(rows)} executed actions reconciled",
                extra={"reconciled": len(rows)},
            )
        mismatched = [found.record for found in settled if found.outcome == MISMATCH]
        if mismatched:
            log.warn(
                "executed actions did not have their effect",
                records=mismatched,
                now=now,
            )
        log.info(
            "executed actions reconciled",
            ok=len(settled) - len(mismatched),
            mismatch=len(mismatched),
            unobserved=unobserved,
            not_due=waiting,
            at_version=version.seq,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return settled
