"""Writing what was worked out, never what was observed: derived rows as one version.

Each written value carries the function or model that produced it as its source, the
kind of fact it is, and the version of the inputs it was worked out from. A property a
source fills stays the sync's to write: derived data never overwrites an observation.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from dta_core.log import Log
from dta_core.manifest.checks import ManifestView
from dta_core.schema import UID_FIELD, FactKind
from dta_core.store import (
    LIVE_BRANCH,
    ChangeSet,
    CommitMeta,
    Provenance,
    StoreError,
    TimeRegression,
    UnknownType,
    VersionRef,
)

from .problems import ApiError
from .registry import ACTOR, FUNCTION_TYPE, MODEL_TYPE

if TYPE_CHECKING:
    from .services import Services

log = Log("api.derived")

MAX_ROWS = 100_000
MAX_BYTES = 16 * 1024 * 1024
KINDS = (FactKind.inferred.value, FactKind.assumed.value, FactKind.simulated.value)
_SOURCES = (FUNCTION_TYPE, MODEL_TYPE)


def too_large(message: str, **detail: Any) -> ApiError:
    log.warn("derived rows refused: too large", **detail)
    return ApiError(message, code="derived.too_large", detail=detail)


def _refuse(code: str, message: str, **detail: Any) -> ApiError:
    log.warn("derived rows refused", code=code, reason=message, **detail)
    return ApiError(message, code=code, detail=detail, extra=detail or None)


def write(
    services: Services,
    *,
    branch: str,
    type_name: str,
    rows: Sequence[Mapping[str, Any]],
    kind: str,
    source: str,
    inputs_version: str,
    confidence: float | None = None,
    t_valid: datetime | None = None,
    records: Mapping[str, list[dict[str, Any]]] | None = None,
    note: str = "",
) -> VersionRef:
    """One version of `branch`, at the time of its head, holding the rows as facts of
    `kind` produced by `source` from the version `inputs_version`.

    `records` are records of the twin about itself written in the same version (the run
    of the function that produced the rows).
    """
    started = time.perf_counter()
    if kind == FactKind.observed.value:
        raise _refuse(
            "derived.observed_refused",
            "observed facts are written by a sync run only; derived rows are inferred, "
            "assumed or simulated",
        )
    if kind not in KINDS:
        raise ApiError(f"kind is one of {', '.join(KINDS)}", code="api.bad_request")
    if branch == LIVE_BRANCH and kind != FactKind.inferred.value:
        raise _refuse(
            "derived.live_refused",
            f"{kind} facts are not written to {LIVE_BRANCH}: assume and simulate on a "
            "scenario's branch",
            kind=kind,
        )
    if len(rows) > MAX_ROWS:
        raise too_large(
            f"a request writes at most {MAX_ROWS} rows; this one holds {len(rows)}",
            rows=len(rows),
        )
    store = services.store
    for attempt in (1, 2):
        head = store.head(branch)
        ontology = services.ontology(head)
        if not ontology.has_type(type_name):
            raise UnknownType(f"unknown type {type_name!r}", detail={"type": type_name})
        object_type = ontology.type(type_name)
        if object_type.meta:
            raise ApiError(
                f"{type_name} is a record of the twin about itself; derived rows are objects "
                "of the twin's own types",
                code="api.bad_request",
            )
        producer = store.get(source, at=head, ontology=ontology)
        if producer is None or producer.type not in _SOURCES:
            raise _refuse(
                "derived.unknown_source",
                f"{source!r} is neither a function nor a model at the head of {branch}",
                source=source,
            )
        try:
            inputs = store.resolve(inputs_version, branch)[0]
        except StoreError as first:
            try:
                # The inputs of rows written to a scenario may be a later version of the
                # live line than the scenario started from.
                inputs = store.resolve(inputs_version, LIVE_BRANCH)[0]
            except StoreError:
                raise _refuse(
                    "derived.bad_inputs_version",
                    f"inputs_version {inputs_version!r} is not a version of {branch} or of "
                    f"{LIVE_BRANCH}",
                    inputs_version=inputs_version,
                ) from first
        written = sorted({name for row in rows for name in row if name != UID_FIELD})
        for index, row in enumerate(rows):
            if not isinstance(row.get(UID_FIELD), str) or not row[UID_FIELD]:
                raise ApiError(f"rows[{index}] has no uid", code="api.bad_request")
        view = ManifestView(services.twin.manifest(at=head, branch=branch))
        mapped = [name for name in written if view.mapped_columns(f"{type_name}.{name}")]
        if mapped:
            raise _refuse(
                "derived.mapped_property",
                f"{type_name}.{mapped[0]} is filled from a source by the manifest's mappings; "
                "only a sync run writes it",
                type=type_name,
                properties=mapped,
            )
        fact = Provenance(
            source=source,
            kind=FactKind(kind),
            t_valid=t_valid or inputs.t,
            confidence=1.0 if confidence is None else confidence,
        )
        upserts: dict[str, list[dict[str, Any]]] = {type_name: [dict(row) for row in rows]}
        own: dict[str, Provenance] = {}
        for name, found in (records or {}).items():
            upserts.setdefault(name, []).extend(found)
            # A record of the twin is the registry's own knowledge, not the function's.
            record_origin = Provenance(source=ACTOR, kind=FactKind.inferred, t_valid=head.t)
            own.update({row[UID_FIELD]: record_origin for row in found})
        changes = ChangeSet(upserts=upserts, provenance=fact, row_provenance=own)
        meta = CommitMeta(
            cause="derived",
            actor=source,
            t=head.t,
            note=note,
            extra={"source": source, "inputs_version": inputs.id, "kind": kind},
        )
        try:
            version = store.commit(changes, meta, branch=branch)
        except TimeRegression:
            if attempt == 2:
                log.error("derived rows could not be written: the branch keeps moving")
                raise
            log.warn("the branch moved on while derived rows were being written; trying again")
            continue
        log.info(
            "derived rows written",
            branch=branch,
            type=type_name,
            rows=len(rows),
            kind=kind,
            source=source,
            inputs_seq=inputs.seq,
            seq=version.seq,
            version=version.id,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        )
        return version
    raise AssertionError("unreachable")
