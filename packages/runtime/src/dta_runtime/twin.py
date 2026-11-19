"""An installed twin: its store, its ontology and its manifest.

The manifest and the ontology are kept in the graph itself. A service is
therefore given nothing but the address of the store: it reads the ontology,
opens the store with it and reads the manifest from there. Installing a twin,
revising its manifest and adding to its ontology all go through one function,
which can be run again at any time and changes only what differs.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import yaml
from dta_core.bundle import Bundle
from dta_core.errors import DtaError, ManifestTooNew
from dta_core.log import Log
from dta_core.manifest import (
    Manifest,
    contract_hash,
    dump_manifest,
    manifest_hash,
    parse_manifest,
)
from dta_core.ontology import build_validator, parse_ontology, type_signature
from dta_core.schema import FactKind, Ontology
from dta_core.store import (
    EPOCH,
    LIVE_BRANCH,
    ChangeSet,
    CommitMeta,
    NotInstalled,
    Provenance,
    Store,
    StoreSettings,
    TimeRegression,
    VersionRef,
    stored_ontology,
)

log = Log("runtime.twin")

_REVISION_TRIES = 3

MANIFEST_TYPE = "TwinManifest"
MANIFEST_UID = "twin_manifest:current"
SOURCE_TYPE = "TwinSource"
# Recorded as the origin of everything written from the manifest.
MANIFEST_ORIGIN = "manifest"


def source_uid(source_id: str) -> str:
    return f"twin_source:{source_id}"


class TwinError(DtaError):
    """The twin cannot be opened or installed as asked."""

    code = "twin.error"


@dataclass
class Twin:
    store: Store
    _manifests: dict[str, Manifest] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def ontology(self) -> Ontology:
        return self.store.ontology

    def manifest_record(self, at: Any = "head", branch: str = LIVE_BRANCH) -> dict[str, Any]:
        """The stored manifest object as of a version: its values without the text."""
        found = self.store.get(MANIFEST_UID, at=at, branch=branch)
        if found is None:
            log.error("no manifest is stored in the twin", at=str(at), branch=branch)
            raise TwinError(
                "the twin holds no manifest at that version", code="twin.not_registered"
            )
        return dict(found.values)

    def manifest(self, at: Any = "head", branch: str = LIVE_BRANCH) -> Manifest:
        """The manifest in force at a version."""
        record = self.manifest_record(at, branch)
        key = record["manifest_hash"]
        with self._lock:
            known = self._manifests.get(key)
        if known is not None:
            return known
        try:
            parsed = parse_manifest(yaml.safe_load(record["content"]), origin="the stored manifest")
        except ManifestTooNew as exc:
            # A newer runtime installed this manifest; this one is older than the twin.
            log.error(
                "the stored manifest is newer than this runtime reads",
                manifest_hash=key,
                manifest_version=exc.detail.get("manifest_version"),
                reads=exc.detail.get("reads"),
            )
            raise TwinError(
                f"the twin's manifest is written in manifest version "
                f"{exc.detail.get('manifest_version')}; this runtime reads versions up to "
                f"{exc.detail.get('reads')}: run the twin from a newer runtime image",
                code="runtime.too_old",
                detail={
                    "manifest_version": exc.detail.get("manifest_version"),
                    "runtime_reads": exc.detail.get("reads"),
                },
            ) from exc
        except (yaml.YAMLError, DtaError) as exc:
            log.error("stored manifest cannot be read", exc=exc, manifest_hash=key)
            raise TwinError(
                f"the manifest stored in the twin cannot be read: {exc}",
                code="twin.manifest_corrupt",
            ) from exc
        with self._lock:
            self._manifests[key] = parsed
        return parsed

    def close(self) -> None:
        self.store.close()


def open_twin(settings: StoreSettings) -> Twin:
    """Open an installed twin with the ontology stored in it."""
    started = time.perf_counter()
    stored = stored_ontology(settings)
    # The row checks are generated from the ontology's own text. Regenerating
    # them must give back the stored ontology exactly; if it does not, this
    # code is of another version than the one that wrote the twin.
    regenerated = parse_ontology(stored.source, origin="the stored ontology").ontology
    if regenerated.hash != stored.hash:
        log.error(
            "stored ontology differs from what this version derives from its text",
            stored=stored.hash,
            derived=regenerated.hash,
        )
        raise TwinError(
            "the twin was written by another version of this software; install it again "
            "with this version to bring its shared types up to date",
            code="twin.ontology_outdated",
            detail={"stored": stored.hash, "derived": regenerated.hash},
        )
    store = Store(settings, stored, build_validator(stored))
    head = store.head()
    log.info(
        "twin opened",
        database=settings.database,
        head_seq=head.seq,
        ontology=stored.hash,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return Twin(store=store)


def _manifest_rows(manifest: Manifest, ontology: Ontology, known_sources: set[str]) -> ChangeSet:
    assert manifest.twin is not None and manifest.twin.id is not None
    record = {
        "uid": MANIFEST_UID,
        "twin_id": manifest.twin.id,
        "name": manifest.twin.name,
        "tier": manifest.contract.tier.value
        if manifest.contract and manifest.contract.tier
        else None,
        "revision": manifest.twin.revision,
        "manifest_hash": manifest_hash(manifest),
        "contract_hash": contract_hash(manifest),
        "content": dump_manifest(manifest),
    }
    declared = manifest.sources or {}
    sources = []
    for source_id, source in declared.items():
        row: dict[str, Any] = {
            "uid": source_uid(source_id),
            "connector": source.connector.value if source.connector else "custom",
            "description": source.description,
            "location": source.location,
        }
        if source_id not in known_sources:
            row["status"] = "ok"
        sources.append(row)
    # A source dropped from the manifest stays in the graph, marked as no longer read.
    for source_id in sorted(known_sources - set(declared)):
        sources.append({"uid": source_uid(source_id), "status": "disabled"})
    return ChangeSet(upserts={MANIFEST_TYPE: [record], SOURCE_TYPE: sources})


def _first_data_time(manifest: Manifest) -> datetime:
    starts = [
        dataset.history_start
        for source in (manifest.sources or {}).values()
        if source.profile is not None
        for dataset in source.profile.datasets.values()
        if dataset.history_start is not None
    ]
    return min(starts) if starts else EPOCH


@dataclass(frozen=True)
class Installed:
    twin: Twin
    version: VersionRef
    # What was done, in order: any of "installed", "migrated", "registered"; empty when
    # the twin already held this manifest and this ontology.
    actions: tuple[str, ...]


def install_twin(
    settings: StoreSettings,
    bundle: Bundle,
    *,
    t: datetime | None = None,
    actor: str = "toolkit",
) -> Installed:
    """Bring the twin in line with a manifest and an ontology.

    Creates the twin when there is none. Otherwise adds to its ontology what
    the new one adds (anything else is refused by the store) and records the
    manifest as a new revision when it differs from the stored one. `t` is the
    version time of a first installation: by default the start of the oldest
    history any source profile reports.
    """
    manifest = bundle.manifest
    if manifest.twin is None or not manifest.twin.id:
        raise TwinError("the manifest names no twin id", code="twin.bundle_invalid")
    ontology = parse_ontology(bundle.ontology_text, origin="the bundle's ontology").ontology
    confirmed = manifest.ontology.type_signature if manifest.ontology else None
    signature = type_signature(ontology)
    if confirmed is not None and confirmed != signature:
        log.error("ontology differs from the confirmed one", confirmed=confirmed, found=signature)
        raise TwinError(
            "the ontology's types are not the ones confirmed in the manifest",
            code="twin.ontology_unconfirmed",
            detail={"confirmed": confirmed, "found": signature},
        )
    validator = build_validator(ontology)
    actions: list[str] = []
    try:
        current: Ontology | None = stored_ontology(settings)
    except NotInstalled:
        current = None

    if current is None:
        store = Store(settings, ontology, validator)
        store.install(manifest.twin.id, t=t or _first_data_time(manifest), actor=actor)
        actions.append("installed")
    elif current.hash == ontology.hash:
        store = Store(settings, ontology, validator)
    else:
        store = Store(settings, current)
        store.migrate(
            ontology,
            CommitMeta(cause="migration", actor=actor, t=store.head().t),
            validator,
        )
        actions.append("migrated")

    twin = Twin(store=store)
    stored = store.get(MANIFEST_UID)
    wanted = manifest_hash(manifest)
    if stored is None or stored.values["manifest_hash"] != wanted:
        known = {
            uid.split(":", 1)[1]
            for uid in store.table(SOURCE_TYPE, columns=[]).column("uid").to_pylist()
        }
        # The revision is written at the head's time. A live twin's sync may commit a
        # later version between reading the head and writing: then it is written again
        # at the new head.
        for attempt in range(1, _REVISION_TRIES + 1):
            head = store.head()
            changes = _manifest_rows(manifest, ontology, known)
            changes.provenance = Provenance(
                source=MANIFEST_ORIGIN, kind=FactKind.assumed, t_valid=head.t
            )
            try:
                store.commit(
                    changes,
                    CommitMeta(
                        cause="registration",
                        actor=actor,
                        t=head.t,
                        note=f"manifest revision {manifest.twin.revision}",
                        extra={"manifest_hash": wanted, "revision": manifest.twin.revision},
                    ),
                )
                break
            except TimeRegression as exc:
                if attempt == _REVISION_TRIES:
                    log.error(
                        "the manifest revision could not be written: the live line keeps moving",
                        exc=exc,
                        twin_id=manifest.twin.id,
                        tries=attempt,
                    )
                    raise
                log.warn(
                    "the live line moved on while the manifest revision was being written; "
                    "writing it again at the new head",
                    twin_id=manifest.twin.id,
                    attempt=attempt,
                    head_t=head.t,
                )
        actions.append("registered")
    version = store.head()
    log.info(
        "twin brought in line with its manifest",
        twin_id=manifest.twin.id,
        actions=actions,
        head_seq=version.seq,
        manifest_hash=wanted,
        ontology=ontology.hash,
    )
    return Installed(twin=twin, version=version, actions=tuple(actions))
