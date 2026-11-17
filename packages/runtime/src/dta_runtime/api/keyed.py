"""Starting work of the compute lane as jobs, each asked-for piece of work started once.

A caller that repeats a request (after a timeout, after a restart of its own) must not
start the work twice: an `Idempotency-Key` seen before gives back the job made for it,
and while a job of the same kind with the same parameters is queued or running, asking
again gives back that job. Other work simply waits its turn in the lane.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from dta_core.log import Log

from .jobs import ACTIVE, COMPUTE_LANE, Job, JobRunner, Work
from .problems import ApiError

log = Log("api.jobs")

KEYS_KEPT = 1_000


class ComputeJobs:
    """Decides, for each request, whether a job of the compute lane is made or found again."""

    def __init__(self, runner: JobRunner, *, keys_kept: int = KEYS_KEPT) -> None:
        self._runner = runner
        self._keys_kept = keys_kept
        self._lock = threading.Lock()
        # Idempotency-Key -> (kind, parameters, the job made for it), oldest first.
        self._keys: OrderedDict[str, tuple[str, dict[str, Any], Job]] = OrderedDict()

    def known(self, kind: str, params: Mapping[str, Any], key: str | None) -> bool:
        """Whether `start` would give back a job that exists rather than make one."""
        asked = dict(params)
        with self._lock:
            if key is not None and key in self._keys:
                return True
            return any(
                document["state"] in ACTIVE and document["params"] == asked
                for document in self._runner.list(kind=kind, lane=COMPUTE_LANE, limit=1_000)
            )

    def start(
        self,
        kind: str,
        params: Mapping[str, Any],
        work: Work,
        key: str | None,
        *,
        again: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """The job document for the request and whether the job was made for it. With
        `again`, a key whose job has ended starts the work anew under the same key: for
        work whose caller knows it did not take effect."""
        asked = dict(params)
        with self._lock:
            if key is not None and key in self._keys:
                known_kind, known, job = self._keys[key]
                if (known_kind, known) != (kind, asked):
                    log.info("an idempotency key was used again with other parameters", job=job.id)
                    raise ApiError(
                        "this Idempotency-Key was used before for another request",
                        code="job.key_reused",
                        detail={"job": job.id},
                    )
                document = self._runner.document(job)
                if not again or document["state"] in ACTIVE:
                    log.info(
                        "work was asked for again; its job is given back", job=job.id, kind=kind
                    )
                    return document, False
                log.warn(
                    "work whose job ended without taking effect is started again",
                    job=job.id,
                    kind=kind,
                    state=document["state"],
                )
                del self._keys[key]
            for document in self._runner.list(kind=kind, lane=COMPUTE_LANE, limit=1_000):
                if document["state"] in ACTIVE and document["params"] == asked:
                    log.info(
                        "the same work is already queued or running", job=document["id"], kind=kind
                    )
                    return document, False
            job = self._runner.submit(kind, asked, work, lane=COMPUTE_LANE, idempotency_key=key)
            if key is not None:
                self._keys[key] = (kind, asked, job)
                while len(self._keys) > self._keys_kept:
                    self._keys.popitem(last=False)
            return self._runner.document(job), True
