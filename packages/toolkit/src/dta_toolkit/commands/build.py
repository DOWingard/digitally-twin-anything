"""`dta build`: from a signed manifest to a running, served twin, step by step.

Each step has a check and a recorded result in `.dta/build.json`. Every step
but one changes only what differs and is its own check of what is there now,
so it runs on every build: a record cannot say whether a container still runs
or a store still holds the twin. `load` is the exception, because reading the
sources again is the expensive part: it is not repeated when it is recorded as
done for the same manifest and ontology file and `install`, which has just
run, finds the twin at the version that load, or the last whole build after
it, left it at (registering and measuring move the version on, not the data).
A store that was emptied or replaced is therefore loaded again without being
asked. A failing step stops the build; nothing already done is undone.

A live twin moves on by itself, so its version says nothing about the load:
its history is loaded only while `install` finds no data written by sync, and
its runs keep it in step afterwards. Before a load writes to a live twin, the
scheduler is stopped and any sync job in progress is waited for, so the load
is the only writer. Its step `sync` starts the scheduler and one run, and
passes only when that run synced every source.

What the twin computes comes after its data: `image` builds the worker's
image when the manifest names requirements, `code` packs and uploads the
twin's own code, `functions` registers what the manifest declares, `models`
evaluates a specification when a candidate of it was registered anew or
changed, when its definition or its question's horizon changed since a build
last evaluated it, or when it has no active model and a candidate never
backtested (every one with `--force` or from `--from models`), and
`questions` measures the questions when anything they rest on changed. Each
asks the API only for what differs, so building a built twin again builds no
image, uploads nothing, registers nothing, evaluates nothing and measures
nothing.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Annotated, Any

import typer
from dta_core.bundle import Bundle
from dta_core.errors import CheckFailed, DtaError
from dta_core.log import Log
from dta_core.manifest import Manifest, manifest_hash
from dta_core.manifest.checks import evaluate_gates, subject_of

from .. import workerimage
from ..acceptance import ApiUnavailable, Context, TwinApi, probe_health, service_url, smoke
from ..envfile import read_env
from ..output import command, emit, session
from ..stack import (
    API_SERVICE,
    SCHEDULER_SERVICE,
    STORE_SERVICE,
    UI_SERVICE,
    WORKER_SERVICE,
    Stack,
    StackError,
)
from ..workspace import Workspace, now, sha256_file
from . import computeops
from .common import fail_check, gate_inputs, ontology_path, scrub_values
from .env import write_example
from .skill import rendered_hash, skill_dir, write_skill
from .syncjobs import (
    BUSY,
    CLOCK_UNAVAILABLE,
    HEALTH_PATH,
    POLL_FIRST_S,
    POLL_MAX_S,
    ApiRefused,
    clock_origin,
    read_json,
    run_summary,
    start_run,
    verdict,
    wait_job,
)

log = Log("commands.build")

STEPS = (
    "scaffold",
    "image",
    "store",
    "install",
    "load",
    "api",
    "contract",
    "sync",
    "code",
    "functions",
    "models",
    "questions",
    "ui",
    "skill",
)
# The only step whose earlier result may stand in for running it again.
REUSABLE_STEPS = frozenset({"load"})
BUILD_STAGE = 6
LAST_REQUIRED_STAGE = 5
RECORD_VERSION = 1
# The record's map of model specification -> what its last evaluation rested on.
BASES_KEY = "evaluated_bases"
INSTALL_TIMEOUT_S = 900.0
# A live twin's scheduler keeps writing while install records a new manifest revision at
# the head it read; a sync committed in between makes that time earlier than the head's.
# Nothing was written then, so install is run again on the new head.
TIME_REGRESSION = "store.time_regression"
INSTALL_TRIES = 3
INSTALL_PAUSE_S = 0.5
# How long a load of a live twin waits for a sync job in progress to end.
LOAD_WAIT_S = 600.0
API = "/api/v1"
LIVE_LOADED = "the twin already holds data; its runs keep it in step"
RUNNING = "running"

OK, FAILED, SKIPPED = "ok", "failed", "skipped"


class StepFailed(Exception):
    """A step ran and its check said no; `evidence` says why."""

    def __init__(self, message: str, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.message = message
        self.evidence = evidence


class Skipped(Exception):
    """A step that does not apply to this manifest."""


def record_path(workspace: Workspace):
    return workspace.meta_dir / "build.json"


def read_record(workspace: Workspace) -> dict[str, Any]:
    path = record_path(workspace)
    if not path.is_file():
        return {"version": RECORD_VERSION, "steps": {}}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warn(
            "build record cannot be read; every step runs", path=str(path), error=type(exc).__name__
        )
        return {"version": RECORD_VERSION, "steps": {}}
    if not isinstance(document, dict) or not isinstance(document.get("steps"), dict):
        log.warn("build record has an unexpected shape; every step runs", path=str(path))
        return {"version": RECORD_VERSION, "steps": {}}
    return document


class Build:
    def __init__(
        self,
        workspace: Workspace,
        manifest: Manifest,
        *,
        start: int,
        force: bool,
        models_timeout: float = computeops.EVALUATE_WAIT_S,
    ) -> None:
        self.workspace = workspace
        self.manifest = manifest
        self.stack = Stack(workspace, manifest)
        self.digest = manifest_hash(manifest)
        path = ontology_path(workspace, manifest)
        if path is None or not path.is_file():
            raise CheckFailed(
                "the manifest's ontology file is missing",
                code="build.not_ready",
                detail={"ontology": str(path) if path else None},
            )
        self.ontology_file = path
        self.ontology_sha = sha256_file(path)
        self.record = read_record(workspace)
        self.start = start
        self.force = force
        self.live = self.stack.plan.live
        self.install_head: dict[str, Any] | None = None
        # What install said: sync has already written data to the twin.
        self.install_loaded = False
        self.models_timeout = models_timeout
        # What this build's own steps found, for the steps after them.
        self.code_sha: str | None = None
        self.registered: dict[str, Any] | None = None
        self.evaluated: list[str] = []
        self.env: dict[str, str] = {}
        self.steps: dict[str, Callable[[], dict[str, Any]]] = {
            "scaffold": self.scaffold,
            "image": self.image,
            "store": self.store,
            "install": self.install,
            "load": self.load,
            "api": self.api,
            "contract": self.contract,
            "sync": self.sync,
            "code": self.code,
            "functions": self.functions,
            "models": self.models,
            "questions": self.questions,
            "ui": self.ui,
            "skill": self.skill,
        }

    # -- the record -----------------------------------------------------------

    def recorded(self, name: str) -> dict[str, Any] | None:
        entry = self.record["steps"].get(name)
        return entry if isinstance(entry, dict) else None

    def reusable(self, name: str) -> tuple[bool, str]:
        """Whether a step's earlier result still holds, and why (not)."""
        if self.force:
            return False, "forced"
        if STEPS.index(name) >= self.start:
            return False, "asked to rerun from an earlier step"
        if name not in REUSABLE_STEPS:
            return False, "runs on every build"
        if self.live:
            # Runs move a live twin's head on, so only what install found can tell.
            if self.install_loaded:
                return True, LIVE_LOADED
            return False, "the twin holds no data written by sync yet"
        entry = self.recorded(name)
        if entry is None:
            return False, "not recorded"
        if entry.get("status") != OK:
            return False, f"recorded as {entry.get('status')}"
        if (
            entry.get("manifest_hash") != self.digest
            or entry.get("ontology_sha256") != self.ontology_sha
        ):
            return False, "the manifest or the ontology file changed"
        head = self.install_head or {}

        def same(after: dict[str, Any]) -> bool:
            return (
                after.get("seq") is not None
                and after.get("seq") == head.get("seq")
                and (not after.get("id") or after.get("id") == head.get("id"))
            )

        # The steps after the load register and measure, which moves the head on without
        # touching the data; where the last whole build left the twin counts as well.
        after = (entry.get("detail") or {}).get("head_after") or {}
        settled = self.record.get("head_after_build") or {}
        if not same(after) and not (settled.get("manifest_hash") == self.digest and same(settled)):
            return False, "the twin is at another version than the last build left it at"
        return True, "done for this manifest and ontology file, and the twin is where it left it"

    def settle(self) -> None:
        """Note where the whole build left the twin, for the next build's load to compare."""
        try:
            with TwinApi(self.manifest, read_env(self.workspace.env_path)) as client:
                head = computeops.head_of(client)
        except DtaError as exc:
            log.warn("where the build left the twin cannot be read", code=exc.code)
            return
        self.record["head_after_build"] = {
            "seq": head.get("seq"),
            "id": head.get("id"),
            "manifest_hash": self.digest,
        }
        with self.workspace.exclusive():
            self.workspace.atomic_write(
                record_path(self.workspace), json.dumps(self.record, indent=2, default=str) + "\n"
            )
        log.info("the build left the twin at its head", seq=head.get("seq"))

    def save(self, name: str, status: str, duration_ms: float, detail: dict[str, Any]) -> None:
        self.record["version"] = RECORD_VERSION
        self.record["steps"][name] = {
            "status": status,
            "at": now().isoformat(),
            "manifest_hash": self.digest,
            "ontology_sha256": self.ontology_sha,
            "duration_ms": duration_ms,
            "detail": detail,
        }
        with self.workspace.exclusive():
            self.workspace.atomic_write(
                record_path(self.workspace), json.dumps(self.record, indent=2, default=str) + "\n"
            )

    # -- the steps --------------------------------------------------------------

    def scaffold(self) -> dict[str, Any]:
        compose_written = self.stack.write()
        example_written = write_example(self.workspace, self.manifest, self.stack)
        absent = self.stack.missing_env()
        if absent:
            raise StepFailed(
                f"{self.stack.env_file.name} does not set {', '.join(absent)}", {"missing": absent}
            )
        self.stack.compose("config", "--quiet")
        return {"compose_written": compose_written, "env_example_written": example_written}

    def image(self) -> dict[str, Any]:
        if not self.manifest.runtime.worker_requirements:
            raise Skipped("runtime.worker_requirements is not set: the worker runs runtime.image")
        if not self.stack.plan.worker:
            raise Skipped("the twin has no registered function and no model: it has no worker")
        result = workerimage.ensure(self.workspace, self.manifest)
        self.stack.use_worker_image(result["tag"])
        result["compose_written"] = self.stack.write()
        return result

    def store(self) -> dict[str, Any]:
        self.stack.up(STORE_SERVICE)
        row = next((r for r in self.stack.services() if r["service"] == STORE_SERVICE), None)
        if row is None or row["state"] != "running" or row["health"] != "healthy":
            raise StepFailed("the store is not running and healthy", {"service": row})
        return {"state": row["state"], "health": row["health"]}

    def install(self) -> dict[str, Any]:
        text = self.ontology_file.read_text(encoding="utf-8")
        bundle = Bundle(manifest=self.manifest, ontology_text=text).to_json()
        attempt = 1
        while True:
            try:
                document = self.stack.runtime("install", input=bundle, timeout=INSTALL_TIMEOUT_S)
                break
            except StackError as exc:
                if exc.code != TIME_REGRESSION or attempt >= INSTALL_TRIES:
                    raise
                log.warn(
                    "install raced a sync run for the head; it runs again on the new head",
                    attempt=attempt,
                    tries=INSTALL_TRIES,
                    pause_s=INSTALL_PAUSE_S,
                    detail=exc.detail,
                )
                attempt += 1
                time.sleep(INSTALL_PAUSE_S)
        version = document.get("version")
        if not isinstance(version, dict) or "seq" not in version:
            raise StepFailed("install returned no version", {"document_keys": sorted(document)})
        self.install_head = {
            "id": version.get("id"),
            "seq": version.get("seq"),
            "t": version.get("t"),
        }
        self.install_loaded = document.get("loaded") is True
        return {
            "actions": list(document.get("actions") or []),
            "version": self.install_head,
            "ontology_hash": document.get("ontology"),
            "loaded": self.install_loaded,
            "attempts": attempt,
        }

    def _quiet_writers(self) -> dict[str, Any]:
        """Stop what writes to a live twin so the load is its only writer.

        The scheduler is stopped; if the API runs and answers, the load waits
        until no sync job is queued or running. On a first build neither runs yet.
        """
        started = time.perf_counter()
        self.stack.stop(SCHEDULER_SERVICE)
        found: dict[str, Any] = {"scheduler": "stopped", "api": None, "waited_for_job": None}
        row = next((r for r in self.stack.services() if r["service"] == API_SERVICE), None)
        if row is None or row["state"] != RUNNING:
            found["api"] = "not running"
            found["waited_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
            log.info("the API is not running; no sync job can be in progress", **found)
            return found
        with TwinApi(self.manifest, self.env) as client:
            pause = POLL_FIRST_S
            while True:
                try:
                    response = client.get(HEALTH_PATH)
                except ApiUnavailable:
                    log.info("the API does not answer; no sync job can be in progress")
                    found["api"] = "not answering"
                    break
                if response.status_code != 200:
                    log.warn(
                        "the API answered health with an error; no sync job can be seen",
                        status=response.status_code,
                    )
                    found["api"] = f"answered {response.status_code}"
                    break
                found["api"] = "answering"
                job = (response.json().get("sync") or {}).get("job")
                if job is None:
                    break
                found["waited_for_job"] = job.get("id")
                waited = time.perf_counter() - started
                if waited >= LOAD_WAIT_S:
                    log.error("a sync job is still in progress; the load is not started", job=job)
                    raise StepFailed(
                        f"a sync job is still {job.get('state')} after {LOAD_WAIT_S:g} s; the "
                        "load would write beside it. Wait for it (dta sync status) and build again",
                        {"job": job, "waited_s": round(waited, 1)},
                    )
                log.info("waiting for the sync job in progress before the load", job=job)
                time.sleep(pause)
                pause = min(pause * 2, POLL_MAX_S)
        found["waited_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        log.info("the live twin has no other writer; the load may start", **found)
        return found

    def load(self) -> dict[str, Any]:
        writers = self._quiet_writers() if self.live else None
        report = self.stack.runtime("load", timeout=None)
        status = report.get("status")
        counts = {
            key: report.get(key)
            for key in (
                "run",
                "kind",
                "versions_written",
                "first_seq",
                "last_seq",
                "rows_read",
                "rows_written",
                "rows_rejected",
                "rejects_location",
                "watermark",
                "duration_ms",
            )
        }
        counts["unmapped_columns"] = len(report.get("unmapped_columns") or [])
        counts["sources"] = {
            sid: {k: v for k, v in (entry or {}).items() if k != "datasets"}
            for sid, entry in (report.get("sources") or {}).items()
        }
        if writers is not None:
            counts["writers"] = writers
        if status != "succeeded":
            raise StepFailed(f"the load run ended as {status}", counts)
        previous = self.recorded("load") or {}
        previous_detail = previous.get("detail") or {}
        written = int(report.get("versions_written") or 0)
        # A rerun that finds nothing new writes no version; the data is then already there
        # when rows were read, a time series has a watermark, or an earlier load wrote it.
        has_data = (
            written > 0
            or int(report.get("rows_read") or 0) > 0
            or report.get("watermark") is not None
            or bool(previous.get("status") == OK and previous_detail.get("has_data"))
        )
        counts["has_data"] = has_data
        if not has_data:
            raise StepFailed("the twin holds no data version after the load", counts)
        if written:
            counts["head_after"] = {"seq": report.get("last_seq"), "id": None}
        else:
            # Nothing new was written: the twin is still where install found it.
            known = self.install_head or {}
            counts["head_after"] = {"seq": known.get("seq"), "id": known.get("id")}
        return counts

    def _worker_up(self) -> dict[str, Any] | None:
        """Start the worker, when the twin has one, and say whether it is healthy."""
        if not self.stack.plan.worker:
            return None
        try:
            self.stack.up(WORKER_SERVICE)
        except StackError as exc:
            log.error("the worker did not come up healthy", exc=exc)
        row = next((r for r in self.stack.services() if r["service"] == WORKER_SERVICE), None)
        found = {
            "image": self.stack.plan.worker_image,
            "state": row["state"] if row else None,
            "health": row["health"] if row else None,
        }
        if row is None or row["state"] != RUNNING or row["health"] != "healthy":
            raise StepFailed(
                f"the worker ({found['image']}) is not running and healthy; see dta logs worker",
                {"worker": found},
            )
        return found

    def _api_container(self) -> str | None:
        row = next((r for r in self.stack.services() if r["service"] == API_SERVICE), None)
        return row.get("id") if row is not None and row.get("state") == RUNNING else None

    def api(self) -> dict[str, Any]:
        # The API reads its notification and its executors once, when it starts. Compose
        # makes its container anew exactly when its configuration changed: the variables
        # it is given, or the label that digests what it sets up at its start. Anything
        # else leaves it running, with the jobs and keys it holds in memory, and leaves
        # the scheduler, which remembers the boundaries it asked for, running too. The
        # worker depends on the API, so bringing the worker up may already make it anew:
        # the container is noted before either.
        before = self._api_container()
        worker = self._worker_up()
        self.stack.up(API_SERVICE)
        after = self._api_container()
        start = {
            "start_config": self.stack.plan.api_start,
            "variables": list(self.stack.plan.api_env),
            "started": before is None,
            "restarted": before is not None and before != after,
        }
        if start["restarted"]:
            log.info(
                "the API was started anew: what it sets up at its start changed",
                start_config=self.stack.plan.api_start,
            )
        with TwinApi(self.manifest, self.env) as client:
            health = client.wait_healthy()
            if health.status_code != 200:
                raise StepFailed("GET /healthz did not answer 200", {"status": health.status_code})
            response = client.get(f"{API}/meta/health")
            if response.status_code != 200:
                raise StepFailed(
                    f"GET {API}/meta/health answered {response.status_code}",
                    {"status": response.status_code},
                )
            body = response.json()
        served = {
            "twin_id": body.get("twin_id"),
            "manifest_hash": (body.get("manifest") or {}).get("hash"),
            "head": body.get("head"),
            "versions": body.get("versions"),
            "api_start": start,
        }
        if worker is not None:
            served["worker"] = {**worker, **((body.get("compute") or {}).get("worker") or {})}
        if served["twin_id"] != self.manifest.twin.id or served["manifest_hash"] != self.digest:
            raise StepFailed(
                "the API serves another twin or another manifest",
                {
                    **served,
                    "expected_twin_id": self.manifest.twin.id,
                    "expected_manifest_hash": self.digest,
                },
            )
        return served

    def contract(self) -> dict[str, Any]:
        ontology = None  # the smoke requests need no ontology
        with TwinApi(self.manifest, self.env) as client:
            response = client.get(f"{API}/openapi.json")
            if response.status_code != 200:
                raise StepFailed(
                    f"GET {API}/openapi.json answered {response.status_code}",
                    {"status": response.status_code},
                )
            document = response.json()
            results = smoke(
                Context(api=client, manifest=self.manifest, ontology=ontology), document
            )
        detail = {"operations": len(document.get("paths") or {}), "requirements": results}
        failing = [entry["requirement"] for entry in results if not entry["ok"]]
        if failing:
            raise StepFailed(
                f"requirement(s) {', '.join(failing)} not answered as expected", detail
            )
        return detail

    def _clock_failed(self, clock: dict[str, Any], detail: dict[str, Any]) -> StepFailed:
        origin = clock_origin(self.manifest)
        log.error("the twin's clock cannot be read", clock=self.manifest.runtime.clock)
        return StepFailed(
            f"the twin's clock cannot be read; it is read from {origin} "
            f"(runtime.clock: {self.manifest.runtime.clock})",
            {**detail, "clock": clock, "clock_origin": origin},
        )

    def sync(self) -> dict[str, Any]:
        if not self.live:
            raise Skipped("contract.tier is static: the twin does not sync")
        self.stack.up(SCHEDULER_SERVICE)
        with TwinApi(self.manifest, self.env) as client:
            try:
                started = start_run(client, {})
            except ApiRefused as exc:
                busy = (exc.detail.get("job") or {}).get("id")
                if exc.code != BUSY or not busy:
                    raise
                # Another kind of run (a replay, a run of some sources) holds the twin.
                log.warn("another sync run is in progress; waiting for it, then asking again")
                wait_job(client, busy)
                started = start_run(client, {})
            job = wait_job(client, started.job["id"])
            found = verdict(job)
            detail: dict[str, Any] = {
                **run_summary(job),
                "job_created": started.created,
                "failing_sources": found.failing,
            }
            if found.code == CLOCK_UNAVAILABLE:
                raise self._clock_failed({"available": False}, {**detail, "code": found.code})
            if not found.ok:
                log.error("the build's sync run did not sync every source", code=found.code)
                raise StepFailed(str(found.message), {**detail, "code": found.code})
            health = read_json(client, HEALTH_PATH)
        clock = health.get("clock") or {}
        state = health.get("sync") or {}
        detail["clock"] = {"kind": clock.get("kind"), "available": clock.get("available")}
        detail["freshness"] = {
            "freshness_s": state.get("freshness_s"),
            "staleness": state.get("staleness"),
            "fresh": state.get("fresh"),
        }
        if not clock.get("available"):
            raise self._clock_failed(detail["clock"], detail)
        row = next((r for r in self.stack.services() if r["service"] == SCHEDULER_SERVICE), None)
        detail["scheduler"] = {"state": row["state"] if row else None}
        if row is None or row["state"] != RUNNING:
            raise StepFailed("the scheduler is not running after it was started", detail)
        log.info(
            "the live twin is in step with its sources",
            run=detail["run"],
            versions_written=detail["versions_written"],
            fresh=state.get("fresh"),
        )
        return detail

    def code(self) -> dict[str, Any]:
        if not self.manifest.runtime.code:
            raise Skipped("runtime.code is not set: the twin has no code of its own")
        packed = computeops.pack_code(self.workspace, self.manifest)
        with TwinApi(self.manifest, self.env) as client:
            result = computeops.upload_code(client, packed)
        self.code_sha = packed.sha256
        return {"directory": self.manifest.runtime.code, **result}

    def _question_ids(self) -> list[str]:
        contract = self.manifest.contract
        return sorted((contract.questions or {}) if contract else {})

    def functions(self) -> dict[str, Any]:
        manifest = self.manifest
        if not (
            manifest.functions
            or manifest.models
            or self._question_ids()
            or manifest.actions
            or manifest.control_loops
        ):
            raise Skipped(
                "the manifest declares no function, model, question, action or control loop"
            )
        # A loop is registered disabled and keeps whether it is enabled when it changes:
        # the build never enables one.
        with TwinApi(self.manifest, self.env) as client:
            self.registered = computeops.register(client, self.code_sha)
        return self.registered

    def _every_spec(self) -> bool:
        return self.force or STEPS.index("models") >= self.start

    def models(self) -> dict[str, Any]:
        specs = sorted(self.manifest.models or {})
        if not specs:
            raise Skipped("the manifest declares no model specification")
        every = self._every_spec()
        touched = computeops.touched_models(self.registered)
        # What each specification's last evaluation by a build rested on, kept across builds.
        bases = {spec: computeops.spec_basis(self.manifest, spec) for spec in specs}
        known = dict(self.record.get(BASES_KEY) or {})
        with TwinApi(self.manifest, self.env) as client:
            states = computeops.spec_states(client, specs)
            reasons = {}
            for spec in specs:
                rebased = spec in known and known[spec] != bases[spec]
                why = computeops.needs_evaluating(states[spec], touched, every, rebased=rebased)
                if why is not None:
                    reasons[spec] = why
            if not reasons:
                log.info("no model specification needs evaluating", specs=specs)
                return {"evaluated": [], "unaccepted": [], "specs": specs}
            head = str(computeops.head_of(client).get("id") or "")
            keys = {
                spec: computeops.job_key("evaluate", self.digest, spec, head) for spec in reasons
            }
            log.info("evaluating model specifications", reasons=reasons)
            results = computeops.evaluate(
                client, list(reasons), keys=keys, limit_s=self.models_timeout
            )
        self.evaluated = [summary["spec"] for summary in results]
        known.update({spec: bases[spec] for spec in self.evaluated if spec in bases})
        self.record[BASES_KEY] = known
        refused = computeops.unaccepted(results)
        for entry in refused:
            log.warn("a model specification has no accepted candidate", **entry)
        return {
            "evaluated": self.evaluated,
            "reasons": reasons,
            "specs": results,
            "unaccepted": refused,
        }

    def questions(self) -> dict[str, Any]:
        if not self._question_ids():
            raise Skipped("the contract declares no question")
        with TwinApi(self.manifest, self.env) as client:
            before = computeops.questions(client)
            never = sorted(q["id"] for q in before if q.get("measured_at") is None)
            # A question registered anew or changed (its tolerance, say) is measured again.
            changed = sorted(computeops.touched_questions(self.registered))
            # So is every question when a function was registered anew or changed (a
            # question rests on its function), and a question that was failing: a build
            # is where a mended twin is judged again.
            functions = sorted(computeops.touched_functions(self.registered))
            failing_before = sorted(q["id"] for q in before if q.get("status") == "failing")
            if not (never or self.evaluated or changed or functions or failing_before):
                log.info("every question was measured and nothing they rest on changed")
                return {"measured": [], "questions": before}
            head = str(computeops.head_of(client).get("id") or "")
            why = {
                "never_measured": never,
                "registered_or_changed": changed,
                "functions_registered_or_changed": functions,
                "failing_before": failing_before,
                "models_evaluated": self.evaluated,
            }
            log.info("measuring the questions", **why)
            ran = computeops.measure(client, key=computeops.job_key("measure", self.digest, head))
            after = computeops.questions(client)
        failing = [q["id"] for q in after if q.get("status") == "failing"]
        if failing:
            log.warn("questions whose measured error is beyond their tolerance", failing=failing)
        return {
            "measured": ran["measured"],
            "why": why,
            "job": ran["job"],
            "questions": after,
            "failing": failing,
        }

    def ui(self) -> dict[str, Any]:
        if not self.manifest.interfaces.ui:
            raise Skipped("interfaces.ui is false")
        self.stack.up(UI_SERVICE)
        status = probe_health(service_url(self.env, self.manifest.interfaces.ui_port_env))
        if status != 200:
            raise StepFailed("GET /healthz on the UI did not answer 200", {"status": status})
        return {"status": status}

    def skill(self) -> dict[str, Any]:
        states = None
        if self._question_ids():
            with TwinApi(self.manifest, self.env) as client:
                states = computeops.questions(client)
        result = write_skill(self.workspace, self.manifest, questions=states)
        if rendered_hash(skill_dir(self.workspace, self.manifest)) != self.digest:
            raise StepFailed(
                "the skill was not written for this manifest", {"path": result["path"]}
            )
        return {key: result[key] for key in ("name", "path", "written", "removed")}

    # -- running ------------------------------------------------------------------

    def run_step(self, name: str) -> dict[str, Any]:
        """Run one step and record it; a failure is recorded, then raised."""
        self.env = read_env(self.workspace.env_path)
        started = time.perf_counter()
        log.info("build step started", step=name)
        try:
            detail = self.steps[name]()
            status = OK
        except Skipped as reason:
            detail, status = {"reason": str(reason)}, SKIPPED
        except StepFailed as exc:
            elapsed = round((time.perf_counter() - started) * 1000.0, 1)
            evidence = scrub_values(
                {"message": exc.message, **exc.evidence}, self.stack.secret_values()
            )
            log.error(
                "build step failed its check", step=name, duration_ms=elapsed, evidence=evidence
            )
            self.save(name, FAILED, elapsed, evidence)
            raise
        except DtaError as exc:
            elapsed = round((time.perf_counter() - started) * 1000.0, 1)
            evidence = scrub_values(
                {"message": exc.message, "code": exc.code, "detail": exc.detail},
                self.stack.secret_values(),
            )
            log.error("build step failed", exc=exc, step=name, code=exc.code, duration_ms=elapsed)
            self.save(name, FAILED, elapsed, evidence)
            raise StepFailed(exc.message, evidence) from exc
        except Exception as exc:
            elapsed = round((time.perf_counter() - started) * 1000.0, 1)
            log.error("build step crashed", exc=exc, step=name, duration_ms=elapsed)
            self.save(
                name,
                FAILED,
                elapsed,
                {"message": "the step crashed", "error_type": type(exc).__name__},
            )
            raise
        elapsed = round((time.perf_counter() - started) * 1000.0, 1)
        detail = scrub_values(detail, self.stack.secret_values())
        self.save(name, status, elapsed, detail)
        log.info("build step finished", step=name, status=status, duration_ms=elapsed)
        return {
            "step": name,
            "action": "ran",
            "status": status,
            "duration_ms": elapsed,
            "detail": detail,
        }


_GATE_HINTS = {
    4: "The manifest changed since it was validated: run dta manifest validate. A change "
    "outside contract needs no new signature.",
    5: "Close the gaps and errors dta manifest validate --complete lists; when the contract "
    "changed, have it agreed and signed again (dta manifest sign).",
}


def _refuse_unless_ready(ctx: typer.Context, workspace: Workspace, manifest: Manifest) -> None:
    outcomes = evaluate_gates(gate_inputs(workspace, manifest))
    unpassed = [o for o in outcomes if o.stage <= LAST_REQUIRED_STAGE and not o.passed]
    if not unpassed:
        return
    log.warn("build refused: gates not passed", gates=[o.name for o in unpassed])
    result = {"gates": [o.as_dict() for o in unpassed]}
    lines = [
        f"  {o.stage} {o.name:<10} {o.verdict.value:<13} {'; '.join(o.reasons)}" for o in unpassed
    ]
    # A revision outside the contract needs the design checked again, not a signature.
    hints = [_GATE_HINTS[o.stage] for o in unpassed if o.stage in _GATE_HINTS]
    if hints:
        result["next"] = hints
        lines += ["", *hints]
    fail_check(
        ctx,
        "build.not_ready",
        f"gate(s) {', '.join(str(o.stage) for o in unpassed)} have not passed",
        result,
        "Not ready to build:\n" + "\n".join(lines),
    )


def _text(outcomes: list[dict[str, Any]]) -> str:
    lines = []
    for entry in outcomes:
        if entry.get("action") == "ran":
            note = f"{entry['duration_ms'] / 1000:.1f} s"
        else:
            note = str(entry.get("reason") or "")
        detail = entry.get("detail") or {}
        if entry.get("status") == SKIPPED:
            note = f"{note}; {detail.get('reason')}"
        elif entry.get("step") == "image" and detail.get("action"):
            note = f"{note}; {detail['action']} {detail.get('tag')}"
        elif entry.get("step") == "api" and (detail.get("api_start") or {}).get("restarted"):
            note = f"{note}; started anew: what it sets up at its start changed"
        elif entry.get("step") == "functions" and detail.get("counts"):
            note = f"{note}; {_registered_text(detail['counts'])}"
        lines.append(
            f"{entry['step']:<9} {entry['action']:<7} {entry['status']!s:<8} {note}".rstrip()
        )
        lines.extend(f"  warning: {text}" for text in _warnings(entry))
    return "\n".join(lines)


def _registered_text(counts: dict[str, dict[str, int]]) -> str:
    """What a registration changed, per section; `nothing changed` when nothing did."""
    parts = []
    for section, found in counts.items():
        changed = [
            f"{found[name]} {name}"
            for name in ("created", "changed", "orphaned")
            if found.get(name) and (name != "orphaned" or section in computeops.ORPHANS_WRITTEN)
        ]
        if changed:
            parts.append(f"{section} {', '.join(changed)}")
    return "; ".join(parts) or "nothing changed"


def _warnings(entry: dict[str, Any]) -> list[str]:
    """What a step that passed still wants read."""
    detail = entry.get("detail") or {}
    found = []
    unaccepted = detail.get("unaccepted") if entry.get("step") == "models" else None
    for spec in unaccepted or []:
        found.append(
            f"model specification {spec['spec']} has no accepted candidate ({spec['reason']}); "
            f"question {spec['question']} is {spec['question_status']}"
        )
    if entry.get("step") == "questions":
        for question in detail.get("questions") or []:
            if question.get("status") == "failing":
                found.append(
                    f"question {question['id']} is failing: measured error "
                    f"{question.get('measured_error')} is beyond its tolerance "
                    f"{question.get('tolerance')}"
                )
    return found


@command
def build(
    ctx: typer.Context,
    from_step: Annotated[
        str | None,
        typer.Option("--from", help=f"Rerun from this step: {', '.join(STEPS)}."),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Rerun every step.")] = False,
    models_timeout: Annotated[
        float,
        typer.Option(
            "--models-timeout",
            min=1.0,
            help="Seconds to wait for each model specification's evaluation.",
        ),
    ] = computeops.EVALUATE_WAIT_S,
) -> None:
    """Build the twin's stack. Needs gates 0 to 5; records the build gate.

    A step reruns from `--from` on as if for the first time: from `models` or
    earlier, every model specification is evaluated again.
    """
    workspace = session(ctx).workspace
    if from_step is not None and from_step not in STEPS:
        raise DtaError(
            f"--from must name a step: {', '.join(STEPS)}",
            code="build.bad_step",
            detail={"steps": list(STEPS), "given": from_step},
        )
    started = time.perf_counter()
    with workspace.exclusive():
        manifest = workspace.load_manifest()
        _refuse_unless_ready(ctx, workspace, manifest)
        start = STEPS.index(from_step) if from_step else len(STEPS)
        run = Build(workspace, manifest, start=start, force=force, models_timeout=models_timeout)
        outcomes: list[dict[str, Any]] = []
        for name in STEPS:
            keep, why = run.reusable(name)
            if keep:
                # A live twin's load is left out on what install found, not on the record.
                status = OK if run.live else (run.recorded(name) or {}).get("status")
                log.info("build step not repeated", step=name, reason=why)
                outcomes.append({"step": name, "action": "reused", "status": status, "reason": why})
                continue
            try:
                outcomes.append(run.run_step(name))
            except StepFailed as exc:
                attempted = [entry["step"] for entry in outcomes] + [name]
                workspace.record_gate(
                    BUILD_STAGE,
                    passed=False,
                    subject_hash=subject_of(BUILD_STAGE, manifest),
                    detail={"failed_step": name},
                )
                result = {
                    "step": name,
                    "evidence": run.record["steps"][name]["detail"],
                    "steps": outcomes,
                    "not_attempted": [step for step in STEPS if step not in attempted],
                    "manifest_hash": run.digest,
                }
                text = _text(outcomes) + f"\n{name:<9} FAILED   {exc.message}"
                fail_check(ctx, "build.failed", f"step {name} failed: {exc.message}", result, text)
        if not run.live:
            run.settle()
        workspace.record_gate(
            BUILD_STAGE,
            passed=True,
            subject_hash=subject_of(BUILD_STAGE, manifest),
            detail={"steps": {entry["step"]: entry["action"] for entry in outcomes}},
        )
    elapsed = round((time.perf_counter() - started) * 1000.0, 1)
    log.info("build finished", duration_ms=elapsed, manifest_hash=run.digest)
    result = {"steps": outcomes, "manifest_hash": run.digest, "duration_ms": elapsed}
    emit(ctx, result, _text(outcomes) + f"\nBuilt in {elapsed / 1000:.1f} s; build gate recorded.")
