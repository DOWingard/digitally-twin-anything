"""What every handler works with: the twin, its graph cache and version selection.

A request resolves its selector once and passes the resulting version to
every store call it makes, so one response describes one version even while
new versions arrive.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from dta_core.schema import Ontology
from dta_core.store import InvalidInput, Selector, Store, VersionRef

from .. import fidelity
from ..actions import Executor
from ..artifacts import ArtifactStore
from ..filters import repair_plus
from ..graph import GraphCache
from ..notify import LOG, Notifier
from ..settings import RuntimeSettings
from ..sync.extract import Connect
from ..twin import Twin
from .acting import KeyedAnswers
from .compute import Compute
from .context import note_version
from .fidelity_work import CHECK_BUDGET_S, CORRECTION_BUDGET_S
from .jobs import JobRunner
from .keyed import ComputeJobs
from .live import Clocks, HeadState, describe_head
from .problems import ApiError
from .rejectfile import RejectsReader
from .schedule import DEFAULT_PROVIDERS, Provider
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
    compute_jobs: ComputeJobs = field(init=False)
    artifacts: ArtifactStore = field(init=False)
    # The one client of the worker; it refuses every run when the twin has no worker.
    compute: Compute = field(init=False)
    # Scenarios a job of the compute lane is running now.
    running_scenarios: set[str] = field(default_factory=set)
    # What the twin tells the outside; logs only, unless the API built one from the manifest.
    notifier: Notifier = field(default_factory=lambda: Notifier(twin_id="twin", kind=LOG))
    # What makes the external calls of actions; None when the executors add-on is off.
    executor: Executor | None = None
    # What reconciliation answered, by Idempotency-Key.
    reconciled: KeyedAnswers = field(default_factory=KeyedAnswers)
    # Who gives the entries of the schedule the scheduler follows.
    schedule: list[Provider] = field(default_factory=lambda: list(DEFAULT_PROVIDERS))
    check_budget_s: float = CHECK_BUDGET_S
    correction_budget_s: float = CORRECTION_BUDGET_S
    # What predicts the check's origins, given the services and the head the check read;
    # None: the worker, as the predict route asks it. Set by tests and replays.
    check_predictor: Callable[[Services, VersionRef], fidelity.Predictor] | None = None
    _ontologies: OrderedDict[str, Ontology] = field(default_factory=OrderedDict)
    _heads: OrderedDict[str, HeadState] = field(default_factory=OrderedDict)
    # Version id -> what was counted there: a version never changes.
    _counts: OrderedDict[str, dict[str, int]] = field(default_factory=OrderedDict)
    _fidelity: OrderedDict[str, dict[str, Any]] = field(default_factory=OrderedDict)
    _first_data: VersionRef | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.sync_jobs = SyncJobs(self.jobs, lambda params: sync_work(self, params))
        self.compute_jobs = ComputeJobs(self.jobs)
        self.artifacts = ArtifactStore(self.settings.artifacts_dir)
        self.compute = Compute(self.settings.worker_url, self.settings.api_token)

    @property
    def store(self) -> Store:
        return self.twin.store

    def close(self) -> None:
        self.compute.close()
        self.notifier.close()
        closing = getattr(self.executor, "close", None)
        if closing is not None:
            closing()

    def interval(self, version: VersionRef) -> timedelta:
        """The step of the twin's grid: the sync interval of the manifest in force at a
        version. Models and scenarios cannot be placed in time without one."""
        manifest = self.twin.manifest(at=version, branch=version.branch)
        found = manifest.sync.interval if manifest.sync else None
        if found is None:
            raise ApiError(
                "the manifest sets no sync interval, so the grid of the twin's models and "
                "scenarios is not known",
                code="twin.no_interval",
            )
        return found

    def first_data(self) -> VersionRef:
        """Where the history of the twin's data begins: the first version a sync wrote, or
        the installation when none has. It lies on every branch, which all start later."""
        with self._lock:
            if self._first_data is not None:
                return self._first_data
        found = self.store.versions(cause="sync", limit=1)
        if not found:
            return self.store.resolve("#0")[0]
        with self._lock:
            self._first_data = found[0].ref
        return found[0].ref

    def fidelity_at(self, version: VersionRef) -> dict[str, Any]:
        """The twin's fidelity state at a version, the incidents open and failed and the
        suspect signals, read once per version: health is polled."""
        with self._lock:
            found = self._fidelity.get(version.id)
            if found is not None:
                self._fidelity.move_to_end(version.id)
                return found
        state = fidelity.state(self.twin, version)
        statuses = [
            found["status"]
            for found in fidelity.incidents(self.twin, at=version, status="unresolved")
        ]
        found = {
            "state": state["state"],
            "since": state["since"],
            "check": state["check"],
            "incidents": {
                "open": sum(1 for status in statuses if status != "failed"),
                "failed": sum(1 for status in statuses if status == "failed"),
            },
            "suspect_signals": len(fidelity.suspects(self.twin, at=version)),
        }
        with self._lock:
            self._fidelity[version.id] = found
            while len(self._fidelity) > HEADS_KEPT:
                self._fidelity.popitem(last=False)
        return found

    def counted(self, version: VersionRef, count: Callable[[], dict[str, int]]) -> dict[str, int]:
        """What `count` gives at a version, counted once per version."""
        with self._lock:
            found = self._counts.get(version.id)
            if found is not None:
                self._counts.move_to_end(version.id)
                return found
        found = count()
        with self._lock:
            self._counts[version.id] = found
            while len(self._counts) > HEADS_KEPT:
                self._counts.popitem(last=False)
        return found

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
