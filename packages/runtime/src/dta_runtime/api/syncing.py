"""Starting sync runs as jobs: one at a time, each asked-for run started once.

Two runs of one twin must never overlap, and a caller that repeats a request
(the scheduler after a restart, a client after a timeout) must not start a
second run. Both are decided here, under one lock: an `Idempotency-Key` seen
before gives back the job made for it, and while a sync job is queued or
running a request for the same run gives back that job. A caller can tell the
two apart by the key the job carries.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.manifest.models import Source

from ..sync import Replay, run
from ..sync.report import FAILED
from .jobs import Job, JobRunner, Outcome, Work
from .problems import ApiError

if TYPE_CHECKING:
    from .services import Services

log = Log("api.sync")

SYNC_KIND = "sync"
KEYS_KEPT = 1_000


@dataclass(frozen=True)
class SyncParams:
    """What a run is asked to do, in one form: sources sorted without repeats."""

    sources: tuple[str, ...] | None = None
    replay: tuple[str, str] | None = None

    @classmethod
    def of(cls, sources: Sequence[str] | None, replay: tuple[str, str] | None) -> SyncParams:
        return cls(tuple(sorted(set(sources))) if sources is not None else None, replay)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sources": list(self.sources) if self.sources is not None else None,
            "replay": {"source": self.replay[0], "dataset": self.replay[1]}
            if self.replay
            else None,
        }


def _datasets(source: Source) -> set[str]:
    """Every name a source's datasets go by in the manifest."""
    names = {mapping.dataset for mapping in (source.mappings or {}).values() if mapping.dataset}
    names.update(source.views)
    if source.profile is not None:
        names.update(source.profile.datasets)
    declared = source.options.get("datasets")
    if isinstance(declared, dict):
        names.update(str(name) for name in declared)
    return names


def check_params(manifest: Manifest, params: SyncParams) -> None:
    """Refuse at once what the run would refuse before reading anything."""
    known = manifest.sources or {}
    unknown = sorted(set(params.sources or ()) - set(known))
    if unknown:
        raise ApiError(
            f"the manifest has no source {', '.join(unknown)}",
            code="sync.unknown_source",
            detail={"sources": unknown},
        )
    if params.replay is not None:
        source_id, dataset = params.replay
        source = known.get(source_id)
        if source is None or dataset not in _datasets(source):
            raise ApiError(
                f"the manifest has no dataset {dataset!r} of a source {source_id!r} to replay",
                code="sync.replay_unknown",
                detail={"source": source_id, "dataset": dataset},
            )


class SyncJobs:
    """Decides, for each request, whether a sync job is made, found again or refused."""

    def __init__(
        self,
        runner: JobRunner,
        work_for: Callable[[SyncParams], Work],
        *,
        keys_kept: int = KEYS_KEPT,
    ) -> None:
        self._runner = runner
        self._work_for = work_for
        self._keys_kept = keys_kept
        self._lock = threading.Lock()
        # Idempotency-Key -> (parameters, the job made for it), oldest first. The job is
        # held here, so a key outlives its job's place among the jobs kept.
        self._keys: OrderedDict[str, tuple[SyncParams, Job]] = OrderedDict()

    def start(self, params: SyncParams, key: str | None) -> tuple[dict[str, Any], bool]:
        """The job document for the request and whether the job was made for it."""
        with self._lock:
            if key is not None and key in self._keys:
                asked, job = self._keys[key]
                if asked != params:
                    log.info("an idempotency key was used again with other parameters", job=job.id)
                    raise ApiError(
                        "this Idempotency-Key was used before with other parameters",
                        code="sync.key_reused",
                        detail={"job": job.id},
                    )
                log.info("a sync run was asked for again; its job is given back", job=job.id)
                return self._runner.document(job), False
            active = self._runner.active(SYNC_KIND)
            if active is not None:
                document = self._runner.document(active)
                if document["params"] != params.as_dict():
                    log.info(
                        "a sync run was refused: another one is queued or running", job=active.id
                    )
                    raise ApiError(
                        "a sync run with other parameters is queued or running; "
                        "ask again when it has finished",
                        code="sync.busy",
                        detail={"job": active.id},
                        extra={"job": document},
                    )
                # The key is not bound to this job, which was made for another request and
                # began before this one: asked again once it has ended, the key starts
                # its own run, which takes what that one was too early for.
                log.info("the same sync run is already queued or running", job=active.id)
                return document, False
            job = self._runner.submit(
                SYNC_KIND, params.as_dict(), self._work_for(params), idempotency_key=key
            )
            self._remember(key, params, job)
            return self._runner.document(job), True

    def _remember(self, key: str | None, params: SyncParams, job: Job) -> None:
        if key is None:
            return
        self._keys[key] = (params, job)
        while len(self._keys) > self._keys_kept:
            self._keys.popitem(last=False)


def sync_work(services: Services, params: SyncParams) -> Work:
    """One run of the sync pipeline with the manifest and the clock in force when it starts."""

    def work() -> Outcome:
        state = services.head_state(services.store.head())
        clock = services.clocks.get(state.manifest, state.manifest_hash)
        report = run(
            services.twin,
            services.settings,
            os.environ,
            clock,
            sources=list(params.sources) if params.sources is not None else None,
            replay=Replay(*params.replay) if params.replay is not None else None,
            connect=services.connect,
        )
        document = report.as_dict()
        if report.status == FAILED:
            error = dict(report.error) if report.error else None
            return Outcome(
                result=document,
                error=error or {"code": "sync.failed", "message": "the run failed"},
            )
        return Outcome(result=document)

    return work
