"""What every handler works with: the twin, its graph cache and version selection.

A request resolves its selector once and passes the resulting version to
every store call it makes, so one response describes one version even while
new versions arrive.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from dta_core.schema import Ontology
from dta_core.store import InvalidInput, Selector, Store, VersionRef

from ..artifacts import ArtifactStore
from ..filters import repair_plus
from ..graph import GraphCache
from ..settings import RuntimeSettings
from ..sync.extract import Connect
from ..twin import Twin
from .context import note_version
from .jobs import JobRunner
from .live import Clocks, HeadState, describe_head
from .problems import ApiError
from .rejectfile import RejectsReader
from .syncing import SyncJobs, sync_work

ONTOLOGIES_KEPT = 256
# Head versions described for health and the sync routes: a few branches, and the
# head a poller saw just before the latest one.
HEADS_KEPT = 8


def _repair_selector(text: str) -> str:
    return "..".join(repair_plus(part) for part in text.split(".."))


@dataclass
class Services:
    twin: Twin
    settings: RuntimeSettings
    graphs: GraphCache
    jobs: JobRunner = field(default_factory=JobRunner)
    clocks: Clocks = field(default_factory=lambda: Clocks(None, os.environ))
    # How a sync run opens a source; None for the real connectors.
    connect: Connect | None = None
    rejects: RejectsReader = field(default_factory=RejectsReader)
    sync_jobs: SyncJobs = field(init=False)
    artifacts: ArtifactStore = field(init=False)
    _ontologies: OrderedDict[str, Ontology] = field(default_factory=OrderedDict)
    _heads: OrderedDict[str, HeadState] = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.sync_jobs = SyncJobs(self.jobs, lambda params: sync_work(self, params))
        self.artifacts = ArtifactStore(self.settings.artifacts_dir)

    @property
    def store(self) -> Store:
        return self.twin.store

    def head_state(self, version: VersionRef) -> HeadState:
        """What is known at a version, read once: a version never changes."""
        with self._lock:
            found = self._heads.get(version.id)
            if found is not None:
                self._heads.move_to_end(version.id)
                return found
        found = describe_head(self.twin, self.ontology(version), version)
        with self._lock:
            self._heads[version.id] = found
            while len(self._heads) > HEADS_KEPT:
                self._heads.popitem(last=False)
        return found

    def resolve(self, at: str, branch: str) -> VersionRef:
        """The one version a selector names; a range is refused."""
        text = _repair_selector(at)
        if Selector.parse(text).is_range:
            raise InvalidInput(
                f"one version is needed here, not a range: {at!r}", code="store.bad_selector"
            )
        version = self.store.resolve(text, branch)[0]
        note_version(version)
        return version

    def resolve_cursor(self, payload: Mapping[str, Any]) -> VersionRef:
        """The version a cursor was issued for."""
        version_id, branch = payload.get("v"), payload.get("b")
        if not isinstance(version_id, str) or not isinstance(branch, str):
            raise ApiError("the cursor is not one this API issued", code="api.bad_cursor")
        version = self.store.resolve(version_id, branch)[0]
        note_version(version)
        return version

    def resolve_range(self, selector: str, branch: str) -> tuple[list[VersionRef], str]:
        """The versions a selector names, oldest first, and the same selection
        written with commit ids so that later calls read exactly these versions."""
        text = _repair_selector(selector)
        parsed = Selector.parse(text)
        versions = self.store.resolve(text, branch)
        note_version(versions[-1])
        if not parsed.is_range:
            return versions, versions[0].id
        # Ending a stepped range at its last selected version selects the same versions.
        pinned = f"{versions[0].id}..{versions[-1].id}"
        if parsed.step != 1:
            pinned += f"/{parsed.step}"
        return versions, pinned

    def ontology(self, version: VersionRef) -> Ontology:
        """The ontology in force at a version; a version never changes, so neither does this."""
        with self._lock:
            found = self._ontologies.get(version.id)
            if found is not None:
                self._ontologies.move_to_end(version.id)
                return found
        found = self.store.ontology_at(version)
        with self._lock:
            self._ontologies[version.id] = found
            while len(self._ontologies) > ONTOLOGIES_KEPT:
                self._ontologies.popitem(last=False)
        return found
