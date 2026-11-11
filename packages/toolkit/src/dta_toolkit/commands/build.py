"""`dta build`: from a signed manifest to a running, served twin, step by step.

Each step has a check and a recorded result in `.dta/build.json`. Every step
but one changes only what differs and is its own check of what is there now,
so it runs on every build: a record cannot say whether a container still runs
or a store still holds the twin. `load` is the exception, because reading the
sources again is the expensive part: it is not repeated when it is recorded as
done for the same manifest and ontology file and `install`, which has just
run, finds the twin at the version that load left it at. A store that was
emptied or replaced is therefore loaded again without being asked. A failing
step stops the build; nothing already done is undone.
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

from ..acceptance import Context, TwinApi, probe_health, service_url, smoke
from ..envfile import read_env
from ..output import command, emit, session
from ..stack import API_SERVICE, STORE_SERVICE, UI_SERVICE, Stack
from ..workspace import Workspace, now, sha256_file
from .common import fail_check, gate_inputs, ontology_path, scrub_values
from .env import write_example
from .skill import rendered_hash, skill_dir, write_skill

log = Log("commands.build")

STEPS = ("scaffold", "store", "install", "load", "api", "contract", "ui", "skill")
# The only step whose earlier result may stand in for running it again.
REUSABLE_STEPS = frozenset({"load"})
BUILD_STAGE = 6
LAST_REQUIRED_STAGE = 5
RECORD_VERSION = 1
INSTALL_TIMEOUT_S = 900.0
API = "/api/v1"

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
        self, workspace: Workspace, manifest: Manifest, *, start: int, force: bool
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
        self.install_head: dict[str, Any] | None = None
        self.env: dict[str, str] = {}
        self.steps: dict[str, Callable[[], dict[str, Any]]] = {
            "scaffold": self.scaffold,
            "store": self.store,
            "install": self.install,
            "load": self.load,
            "api": self.api,
            "contract": self.contract,
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
        after = (entry.get("detail") or {}).get("head_after") or {}
        head = self.install_head or {}
        if (
            after.get("seq") is None
            or after.get("seq") != head.get("seq")
            or (after.get("id") and after.get("id") != head.get("id"))
        ):
            return False, "the twin is at another version than the last load left it at"
        return True, "done for this manifest and ontology file, and the twin is where it left it"

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

    def store(self) -> dict[str, Any]:
        self.stack.up(STORE_SERVICE)
        row = next((r for r in self.stack.services() if r["service"] == STORE_SERVICE), None)
        if row is None or row["state"] != "running" or row["health"] != "healthy":
            raise StepFailed("the store is not running and healthy", {"service": row})
        return {"state": row["state"], "health": row["health"]}

    def install(self) -> dict[str, Any]:
        text = self.ontology_file.read_text(encoding="utf-8")
        bundle = Bundle(manifest=self.manifest, ontology_text=text).to_json()
        document = self.stack.runtime("install", input=bundle, timeout=INSTALL_TIMEOUT_S)
        version = document.get("version")
        if not isinstance(version, dict) or "seq" not in version:
            raise StepFailed("install returned no version", {"document_keys": sorted(document)})
        self.install_head = {
            "id": version.get("id"),
            "seq": version.get("seq"),
            "t": version.get("t"),
        }
        return {
            "actions": list(document.get("actions") or []),
            "version": self.install_head,
            "ontology_hash": document.get("ontology"),
        }

    def load(self) -> dict[str, Any]:
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

    def api(self) -> dict[str, Any]:
        self.stack.up(API_SERVICE)
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
        }
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

    def ui(self) -> dict[str, Any]:
        if not self.manifest.interfaces.ui:
            raise Skipped("interfaces.ui is false")
        self.stack.up(UI_SERVICE)
        status = probe_health(service_url(self.env, self.manifest.interfaces.ui_port_env))
        if status != 200:
            raise StepFailed("GET /healthz on the UI did not answer 200", {"status": status})
        return {"status": status}

    def skill(self) -> dict[str, Any]:
        result = write_skill(self.workspace, self.manifest)
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
        took = f"{entry['duration_ms'] / 1000:.1f} s" if entry.get("action") == "ran" else ""
        lines.append(
            f"{entry['step']:<9} {entry['action']:<7} {entry['status']:<8} {took}".rstrip()
        )
    return "\n".join(lines)


@command
def build(
    ctx: typer.Context,
    from_step: Annotated[
        str | None,
        typer.Option("--from", help=f"Rerun from this step: {', '.join(STEPS)}."),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Rerun every step.")] = False,
) -> None:
    """Build the twin's stack. Needs gates 0 to 5; records the build gate."""
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
        run = Build(workspace, manifest, start=start, force=force)
        outcomes: list[dict[str, Any]] = []
        for name in STEPS:
            keep, why = run.reusable(name)
            if keep:
                entry = run.recorded(name) or {}
                log.info("build step not repeated", step=name, reason=why)
                outcomes.append(
                    {"step": name, "action": "reused", "status": entry.get("status"), "reason": why}
                )
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
