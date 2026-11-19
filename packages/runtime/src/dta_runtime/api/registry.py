"""The registries of a twin: its functions, models and questions as objects of its graph.

The manifest declares what the contract needs; what the twin runs is what these records
say. `register` brings them in line with the manifest in one version; the routes add
and retire records one at a time. Every write here is a registration: a version of the
live line at the time of its head, since registering brings no newer data.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from dta_core.families import FAMILIES
from dta_core.functions import BUILTINS, REGISTERED_ENTRY
from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.manifest.checks import ManifestView, ontology_findings
from dta_core.manifest.checks.references import function_entry
from dta_core.manifest.models import FunctionSpec
from dta_core.schema import FactKind, Ontology
from dta_core.store import (
    LIVE_BRANCH,
    ChangeSet,
    CommitMeta,
    Provenance,
    TimeRegression,
    VersionRef,
)
from pydantic import TypeAdapter

from ..actions import registration as action_registration
from ..protocol import FamilyRef, FeatureRef
from .problems import ApiError

if TYPE_CHECKING:
    from .services import Services

log = Log("api.registry")

FUNCTION_TYPE = "TwinFunction"
RUN_TYPE = "TwinFunctionRun"
MODEL_TYPE = "TwinModel"
BACKTEST_TYPE = "TwinBacktest"
QUESTION_TYPE = "TwinQuestion"
ARTIFACT_TYPE = "TwinArtifact"
SCENARIO_TYPE = "TwinScenario"
FUNCTION_SCOPE = "twin_function"
RUN_SCOPE = "twin_function_run"
MODEL_SCOPE = "twin_model"
BACKTEST_SCOPE = "twin_backtest"
QUESTION_SCOPE = "twin_question"
ARTIFACT_SCOPE = "twin_artifact"
# Recorded as the actor of the versions written here and as the origin of their values.
ACTOR = "registry"
MANIFEST = "manifest"
RUNTIME = "runtime"
NOT_MEASURED = "not measured yet"
CODE_MEDIA = "application/zip"
# A function that states no latency budget is given this long.
DEFAULT_LATENCY_S = 30.0
# What a change to resets a model: its fitted state was made for another one. What a
# prediction reads besides its features (`reads`) is not: declaring it changes no fit.
_MODEL_IDENTITY = ("family", "code", "params", "target", "features")
_MODEL_STATE = ("fitted", "fitted_from", "fitted_to", "fitted_at_version", "validity")
_DURATION = TypeAdapter(timedelta)
_lock = threading.Lock()


def function_uid(function_id: str) -> str:
    return (
        function_id
        if function_id.startswith(f"{FUNCTION_SCOPE}:")
        else (f"{FUNCTION_SCOPE}:{function_id}")
    )


def model_uid(spec: str, candidate: str | None = None) -> str:
    """`twin_model:<spec>/<candidate>`; a uid, with or without its scope, as it is."""
    if candidate is not None:
        return f"{MODEL_SCOPE}:{spec}/{candidate}"
    return spec if spec.startswith(f"{MODEL_SCOPE}:") else f"{MODEL_SCOPE}:{spec}"


def question_uid(question_id: str) -> str:
    return (
        question_id
        if question_id.startswith(f"{QUESTION_SCOPE}:")
        else (f"{QUESTION_SCOPE}:{question_id}")
    )


def artifact_uid(sha256: str) -> str:
    return f"{ARTIFACT_SCOPE}:{sha256}"


def bare(uid: str | None) -> str | None:
    """A uid without its scope: the id of a function or question, the hash of an artifact."""
    return None if uid is None else uid.split(":", 1)[-1]


def duration_text(value: timedelta | None) -> str | None:
    return None if value is None else str(_DURATION.dump_python(value, mode="json"))


def finite(value: Any) -> Any:
    """The same value with every number that is not finite as null: JSON holds no NaN,
    and an error measure that could not be computed is unknown, not a number."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {name: finite(found) for name, found in value.items()}
    if isinstance(value, list | tuple):
        return [finite(found) for found in value]
    return value


def as_json(value: Any) -> str:
    return json.dumps(finite(value), sort_keys=True, separators=(",", ":"), default=str)


def from_json(text: Any, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        log.warn("a record holds text that is not JSON; it is given as it is")
        return text


# -- reading -----------------------------------------------------------------------------


def rows(
    services: Services,
    type_name: str,
    version: VersionRef,
    filters: Sequence[tuple[str, str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The records of a type at a version, in uid order; none where the type is not known."""
    ontology = services.ontology(version)
    if not ontology.has_type(type_name):
        return []
    table = services.store.table(
        type_name, filter=list(filters) if filters else None, at=version, ontology=ontology
    )
    return sorted(table.to_pylist(), key=lambda row: row["uid"])


def one(services: Services, type_name: str, uid: str, version: VersionRef) -> dict[str, Any] | None:
    found = services.store.get(uid, at=version, ontology=services.ontology(version))
    if found is None or found.type != type_name:
        return None
    return {"uid": found.uid, **found.values}


def function_or_404(services: Services, function_id: str, version: VersionRef) -> dict[str, Any]:
    found = one(services, FUNCTION_TYPE, function_uid(function_id), version)
    if found is None:
        raise ApiError(
            f"no function {bare(function_uid(function_id))!r} is registered at version "
            f"#{version.seq} of {version.branch}",
            code="function.not_found",
        )
    return found


def model_or_404(services: Services, uid: str, version: VersionRef) -> dict[str, Any]:
    found = one(services, MODEL_TYPE, model_uid(uid), version)
    if found is None:
        raise ApiError(
            f"no model {model_uid(uid)!r} is registered at version #{version.seq} of "
            f"{version.branch}",
            code="model.not_found",
        )
    return found


def question_or_404(services: Services, question_id: str, version: VersionRef) -> dict[str, Any]:
    found = one(services, QUESTION_TYPE, question_uid(question_id), version)
    if found is None:
        raise ApiError(
            f"no question {bare(question_uid(question_id))!r} is registered at version "
            f"#{version.seq} of {version.branch}",
            code="question.not_found",
        )
    return found


def spec_of(record: Mapping[str, Any]) -> FunctionSpec:
    """A function's record as the specification the function package resolves against."""
    budget = record.get("latency_budget_ms")
    return FunctionSpec(
        description=record.get("description"),
        kind=record["kind"],
        entry=record["entry"],
        params=from_json(record.get("params"), {}),
        latency_budget=None if budget is None else timedelta(milliseconds=budget),
        trigger=record.get("trigger") or "on_request",
        env=list(record.get("env") or []),
    )


def latency_s(record: Mapping[str, Any]) -> float:
    budget = record.get("latency_budget_ms")
    return DEFAULT_LATENCY_S if budget is None else budget / 1000.0


def family_ref(record: Mapping[str, Any]) -> FamilyRef:
    """A shipped family by its name; the twin's own by its code and entry."""
    if record.get("code"):
        return FamilyRef(artifact=bare(record["code"]), entry=record["family"])
    return FamilyRef(name=record["family"])


def features_of(record: Mapping[str, Any]) -> list[FeatureRef]:
    """A model's features: each the JSON of a feature, or a bare `Type.property`."""
    found = []
    for text in record.get("features") or []:
        if text.lstrip().startswith("{"):
            found.append(FeatureRef.model_validate_json(text))
        else:
            found.append(FeatureRef(property=text))
    return found


# -- documents ---------------------------------------------------------------------------


def function_document(record: Mapping[str, Any], detailed: bool = False) -> dict[str, Any]:
    fixed = from_json(record.get("params"), {})
    document = {
        "id": bare(record["uid"]),
        "uid": record["uid"],
        "kind": record["kind"],
        "entry": record["entry"],
        "description": record.get("description"),
        "params": fixed,
        "latency_budget_ms": record.get("latency_budget_ms"),
        "trigger": record.get("trigger"),
        "env": list(record.get("env") or []),
        "artifact": bare(record.get("artifact")),
        "origin": record["origin"],
        "status": record["status"],
    }
    if not detailed:
        return document
    builtin = BUILTINS.get(record["entry"]) if record["kind"] == "builtin" else None
    if builtin is not None:
        document["parameters"] = [
            {
                "name": parameter.name,
                "kind": parameter.kind.value,
                "description": parameter.description,
                "required": parameter.required,
                "default": parameter.default,
                "choices": list(parameter.choices),
                "fixed": parameter.name in fixed,
            }
            for parameter in builtin.parameters
        ]
    else:
        # Code of the twin's own has no table of parameters: only what is fixed is known.
        document["parameters"] = [
            {
                "name": name,
                "kind": None,
                "description": None,
                "required": False,
                "default": None,
                "choices": [],
                "fixed": True,
            }
            for name in sorted(fixed)
        ]
    return document


def model_document(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "uid": record["uid"],
        "spec": record["spec"],
        "candidate": record["candidate"],
        "family": record["family"],
        "code": bare(record.get("code")),
        "params": from_json(record.get("params"), {}),
        "question": record.get("question"),
        "target": record["target"],
        "features": list(record.get("features") or []),
        "reads": None if record.get("reads") is None else list(record["reads"]),
        "interventional": bool(record["interventional"]),
        "origin": record["origin"],
        "status": record["status"],
        "fitted": bare(record.get("fitted")),
        "fitted_from": record.get("fitted_from"),
        "fitted_to": record.get("fitted_to"),
        "fitted_at_version": record.get("fitted_at_version"),
        "validity": from_json(record.get("validity")),
    }


def backtest_document(record: Mapping[str, Any]) -> dict[str, Any]:
    document = dict(record)
    document["scores"] = from_json(record.get("scores"), {})
    document["residuals"] = bare(record.get("residuals"))
    return document


def question_document(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": record["question"],
        "uid": record["uid"],
        "status": record["status"],
        "reason": record.get("reason"),
        "metric": record.get("metric"),
        "measured_error": record.get("measured_error"),
        "tolerance": record.get("tolerance"),
        "unit": record.get("unit"),
        "basis": record.get("basis"),
        "function": bare(record.get("function")),
        "model": record.get("model"),
        "backtest": record.get("backtest"),
        "measured_at": record.get("measured_at"),
    }


# -- writing -----------------------------------------------------------------------------


def commit(
    services: Services,
    upserts: Mapping[str, list[dict[str, Any]]],
    *,
    note: str,
    extra: Mapping[str, Any] | None = None,
    t_valid: Any = None,
) -> VersionRef:
    """One version of the live line holding records of the twin about itself, at the time
    of its head. `t_valid` is the data time the records speak of, when that is earlier."""
    store = services.store
    for attempt in (1, 2):
        head = store.head()
        changes = ChangeSet(
            upserts={name: found for name, found in upserts.items() if found},
            provenance=Provenance(source=ACTOR, kind=FactKind.inferred, t_valid=t_valid or head.t),
        )
        meta = CommitMeta(
            cause="registration", actor=ACTOR, t=head.t, note=note, extra=dict(extra or {})
        )
        try:
            return store.commit(changes, meta)
        except TimeRegression as exc:
            # A sync wrote a later version between reading the head and committing.
            if attempt == 2:
                log.error(
                    "records could not be written: the live line keeps moving", exc=exc, note=note
                )
                raise
            log.warn(
                "the live line moved on while records were being written; writing them "
                "again at the new head",
                note=note,
                attempt=attempt,
            )
    raise AssertionError("unreachable")


def artifact_row(services: Services, sha256: str, media_type: str, description: str) -> dict:
    """The record of a file the artifact store holds; refused when it holds none."""
    path = services.artifacts.find(sha256)
    return {
        "uid": artifact_uid(sha256),
        "sha256": sha256,
        "media_type": media_type,
        "size_bytes": path.stat().st_size,
        "location": services.artifacts.location(sha256),
        "description": description,
    }


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, list | tuple) or isinstance(right, list | tuple):
        return list(left or []) == list(right or [])
    return left == right


def _changes(wanted: Mapping[str, Any], stored: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: value
        for name, value in wanted.items()
        if name != "uid" and not _same(value, stored.get(name))
    }


def _is_code(family: str | None) -> bool:
    return bool(family) and family not in FAMILIES and bool(REGISTERED_ENTRY.match(family or ""))


def _function_row(function_id: str, spec: FunctionSpec, code: str | None) -> dict[str, Any]:
    budget = spec.latency_budget
    return {
        "uid": function_uid(function_id),
        "kind": spec.kind,
        "entry": spec.entry,
        "artifact": artifact_uid(code) if spec.kind == "registered" and code else None,
        "description": spec.description,
        "params": as_json(spec.params),
        "complexity": spec.complexity,
        "latency_budget_ms": None
        if budget is None
        else max(1, round(budget / timedelta(milliseconds=1))),
        "trigger": spec.trigger,
        "interval": duration_text(spec.interval),
        "env": list(spec.env),
        "origin": MANIFEST,
        "status": "active",
    }


def _wanted(manifest: Manifest, code: str | None) -> tuple[dict, dict, dict]:
    """The records the manifest asks for, by uid: functions, models, questions."""
    view = ManifestView(manifest)
    functions = {
        function_uid(fid): _function_row(fid, spec, code)
        for fid, spec in view.functions.items()
        if spec.kind is not None and spec.entry is not None
    }
    models: dict[str, dict[str, Any]] = {}
    for spec_id, spec in view.models.items():
        for candidate_id, candidate in (spec.candidates or {}).items():
            if candidate.family is None or spec.target is None:
                continue
            uid = model_uid(spec_id, candidate_id)
            models[uid] = {
                "uid": uid,
                "spec": spec_id,
                "candidate": candidate_id,
                "family": candidate.family,
                "code": artifact_uid(code) if _is_code(candidate.family) and code else None,
                "params": as_json(candidate.params),
                "question": spec.question,
                "target": spec.target,
                "features": list(spec.features or []),
                "reads": None if spec.reads is None else list(spec.reads),
                "interventional": spec.interventional,
                "origin": MANIFEST,
            }
    questions: dict[str, dict[str, Any]] = {}
    for question_id, question in view.questions.items():
        tolerance = question.tolerance
        uid = question_uid(question_id)
        questions[uid] = {
            "uid": uid,
            "question": question_id,
            "metric": tolerance.metric.value if tolerance and tolerance.metric else None,
            "tolerance": tolerance.value if tolerance else None,
            "unit": tolerance.unit if tolerance else None,
            "function": function_uid(question.function)
            if question.function and function_uid(question.function) in functions
            else None,
        }
    return functions, models, questions


def needs_code(manifest: Manifest) -> bool:
    view = ManifestView(manifest)
    return any(spec.kind == "registered" for spec in view.functions.values()) or any(
        _is_code(candidate.family)
        for spec in view.models.values()
        for candidate in (spec.candidates or {}).values()
    )


def register(services: Services, code: str | None) -> dict[str, Any]:
    """Bring the registries, the action types and the control loops in line with the
    manifest at the head of the live line: what was created, changed and left, and the
    version that holds it. One version when anything differs, none otherwise."""
    started = time.perf_counter()
    with _lock:
        head = services.store.head()
        manifest = services.twin.manifest(at=head)
        if needs_code(manifest) and code is None:
            log.warn("registration refused: the manifest needs code and none was given")
            raise ApiError(
                "the manifest names a function or a model family supplied as code; upload "
                "the packed code with POST /artifacts and pass its hash as `code`",
                code="register.code_missing",
            )
        upserts: dict[str, list[dict[str, Any]]] = {}
        if code is not None:
            row = artifact_row(services, code, CODE_MEDIA, "code of the twin")
            if one(services, ARTIFACT_TYPE, row["uid"], head) is None:
                upserts[ARTIFACT_TYPE] = [row]
        functions, models, questions = _wanted(manifest, code)
        stored = {
            name: {row["uid"]: row for row in rows(services, name, head)}
            for name in (FUNCTION_TYPE, MODEL_TYPE, QUESTION_TYPE)
        }
        answer: dict[str, dict[str, list[str]]] = {
            "functions": {"created": [], "changed": [], "unchanged": [], "disabled": []},
            "models": {"created": [], "changed": [], "unchanged": [], "retired": []},
            "questions": {"created": [], "changed": [], "unchanged": [], "orphaned": []},
        }

        def place(section: str, type_name: str, uid: str, row: dict | None, new: bool) -> None:
            if row is None:
                answer[section]["unchanged"].append(uid)
                return
            answer[section]["created" if new else "changed"].append(uid)
            upserts.setdefault(type_name, []).append(row)

        for uid, wanted in functions.items():
            known = stored[FUNCTION_TYPE].get(uid)
            if known is None:
                place("functions", FUNCTION_TYPE, uid, wanted, True)
                continue
            changed = _changes(wanted, known)
            place(
                "functions", FUNCTION_TYPE, uid, {"uid": uid, **changed} if changed else None, False
            )
        for uid, known in stored[FUNCTION_TYPE].items():
            if (
                uid not in functions
                and known["origin"] == MANIFEST
                and known["status"] != "disabled"
            ):
                answer["functions"]["disabled"].append(uid)
                upserts.setdefault(FUNCTION_TYPE, []).append({"uid": uid, "status": "disabled"})

        for uid, wanted in models.items():
            known = stored[MODEL_TYPE].get(uid)
            if known is None:
                place("models", MODEL_TYPE, uid, {**wanted, "status": "candidate"}, True)
                continue
            changed = _changes(wanted, known)
            if any(name in changed for name in _MODEL_IDENTITY):
                # The fitted state was made for another family, other settings or other
                # inputs: it is dropped, and the candidate stands to be evaluated again.
                changed.update(dict.fromkeys(_MODEL_STATE), status="candidate")
            elif known["status"] == "retired":
                changed["status"] = "candidate"
            place("models", MODEL_TYPE, uid, {"uid": uid, **changed} if changed else None, False)
        for uid, known in stored[MODEL_TYPE].items():
            if uid not in models and known["origin"] == MANIFEST and known["status"] != "retired":
                answer["models"]["retired"].append(uid)
                upserts.setdefault(MODEL_TYPE, []).append({"uid": uid, "status": "retired"})

        for uid, wanted in questions.items():
            known = stored[QUESTION_TYPE].get(uid)
            if known is None:
                new = {**wanted, "status": "provisional", "reason": NOT_MEASURED}
                place("questions", QUESTION_TYPE, uid, new, True)
                continue
            changed = _changes(wanted, known)
            place(
                "questions", QUESTION_TYPE, uid, {"uid": uid, **changed} if changed else None, False
            )
        answer["questions"]["orphaned"] = sorted(set(stored[QUESTION_TYPE]) - set(questions))

        # Action types and control loops go into the same version as the functions a
        # loop's policy names.
        acting = action_registration.rows(manifest, action_registration.known(services.twin, head))
        for type_name, found_rows in acting.upserts.items():
            upserts.setdefault(type_name, []).extend(found_rows)
        answer.update(acting.answer)

        version = head
        written = sum(len(found) for found in upserts.values())
        if written:
            version = commit(
                services,
                upserts,
                note="registries brought in line with the manifest",
                extra={"registered": written, "code": code},
            )
    log.info(
        "registries brought in line with the manifest",
        version=version.id,
        seq=version.seq,
        committed=bool(written),
        counts={
            section: {name: len(found) for name, found in parts.items()}
            for section, parts in answer.items()
        },
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return {**answer, "version": version}


# -- registering while the twin runs -----------------------------------------------------


def _refuse(code: str, message: str, errors: Iterable[dict[str, Any]] = ()) -> ApiError:
    found = list(errors)
    log.info("registration refused", code=code, reason=message)
    return ApiError(message, code=code, extra={"errors": found} if found else None)


def _check_property(ontology: Ontology, ref: str, member: str) -> None:
    type_name, dot, name = ref.partition(".")
    if not dot or not type_name or not name:
        raise _refuse(
            "ontology.unknown_property", f"{member}: {ref!r} is not written Type.property"
        )
    if type_name in ontology.abstract:
        concrete = list(ontology.abstract[type_name])
    elif ontology.has_type(type_name):
        concrete = [type_name]
    else:
        raise _refuse("ontology.unknown_type", f"{member}: the ontology has no type {type_name}")
    if not all(ontology.type(found).has_field(name) for found in concrete):
        raise _refuse("ontology.unknown_property", f"{member}: {type_name} has no property {name}")


def add_function(
    services: Services,
    *,
    function_id: str,
    entry: str,
    params: Mapping[str, Any],
    description: str | None,
    latency_budget: timedelta | None,
    env: Sequence[str],
    artifact: str | None,
) -> tuple[dict[str, Any], VersionRef]:
    """Register one function while the twin runs, checked as the manifest's are."""
    kind = "registered" if artifact is not None or ":" in entry else "builtin"
    spec = FunctionSpec(
        description=description,
        kind=kind,
        entry=entry,
        params=dict(params),
        latency_budget=latency_budget,
        env=list(env),
    )
    base = f"functions.{function_id}"
    manifest = Manifest(functions={function_id: spec})
    issues = list(function_entry(ManifestView(manifest)))
    if issues:
        errors = [{"path": issue.path, "message": issue.message} for issue in issues]
        raise _refuse("ref.function_entry", f"{issues[0].path}: {issues[0].message}", errors)
    if kind == "registered" and artifact is None:
        raise _refuse(
            "ref.function_entry",
            f"{base}.entry: {entry!r} names code; give the hash of its code as `artifact`",
        )
    with _lock:
        head = services.store.head()
        findings = ontology_findings(manifest, services.ontology(head), "")
        if findings:
            errors = [found.as_dict() for found in findings]
            first = findings[0]
            raise _refuse(first.code, f"{first.path}: {first.message}", errors)
        uid = function_uid(function_id)
        if services.store.get(uid, at=head) is not None:
            raise _refuse("function.exists", f"a function {function_id!r} is registered already")
        upserts: dict[str, list[dict[str, Any]]] = {}
        if artifact is not None:
            row = artifact_row(services, artifact, CODE_MEDIA, f"code of function {function_id}")
            if one(services, ARTIFACT_TYPE, row["uid"], head) is None:
                upserts[ARTIFACT_TYPE] = [row]
        upserts[FUNCTION_TYPE] = [{**_function_row(function_id, spec, artifact), "origin": RUNTIME}]
        version = commit(services, upserts, note=f"function {function_id} registered")
    log.info("function registered", function=function_id, entry=entry, kind=kind, seq=version.seq)
    found = one(services, FUNCTION_TYPE, uid, version)
    assert found is not None
    return found, version


def disable_function(services: Services, function_id: str) -> tuple[dict[str, Any], VersionRef]:
    with _lock:
        head = services.store.head()
        record = function_or_404(services, function_id, head)
        if record["origin"] == MANIFEST:
            raise _refuse(
                "function.from_manifest",
                f"{bare(record['uid'])} is declared in the manifest; take it out of the "
                "manifest and register again",
            )
        if record["status"] == "disabled":
            return record, head
        version = commit(
            services,
            {FUNCTION_TYPE: [{"uid": record["uid"], "status": "disabled"}]},
            note=f"function {bare(record['uid'])} disabled",
        )
    log.info("function disabled", function=record["uid"], seq=version.seq)
    return {**record, "status": "disabled"}, version


def add_model(
    services: Services,
    *,
    spec: str,
    candidate: str,
    family: str | None,
    artifact: str | None,
    entry: str | None,
    params: Mapping[str, Any],
    target: str,
    features: Sequence[str],
    question: str | None,
    interventional: bool,
    reads: Sequence[str] | None = None,
) -> tuple[dict[str, Any], VersionRef]:
    """Register one candidate while the twin runs. `reads`: what its prediction reads
    from the twin besides its features; None when it declares none."""
    code = artifact is not None and entry is not None
    if (family is not None) == code or (artifact is None) != (entry is None):
        raise ApiError(
            "a model names a shipped `family`, or code by `artifact` and `entry`",
            code="api.bad_request",
        )
    if family is not None and family not in FAMILIES:
        raise _refuse(
            "ref.model_family",
            f"'{family}' is not a shipped family ({', '.join(FAMILIES)}); code is given by "
            "`artifact` and `entry`",
        )
    with _lock:
        head = services.store.head()
        ontology = services.ontology(head)
        _check_property(ontology, target, "target")
        for index, feature in enumerate(features):
            try:
                ref = (
                    FeatureRef.model_validate_json(feature)
                    if feature.lstrip().startswith("{")
                    else FeatureRef(property=feature)
                )
            except ValueError as exc:
                raise ApiError(
                    f"features[{index}] is neither Type.property nor the JSON of a feature",
                    code="api.bad_request",
                ) from exc
            _check_property(ontology, ref.property, f"features[{index}]")
        for index, read in enumerate(reads or []):
            _check_property(ontology, read, f"reads[{index}]")
        uid = model_uid(spec, candidate)
        if services.store.get(uid, at=head) is not None:
            raise _refuse("model.exists", f"a model {uid} is registered already")
        if question is not None:
            question_or_404(services, question, head)
        upserts: dict[str, list[dict[str, Any]]] = {}
        if artifact is not None:
            row = artifact_row(services, artifact, CODE_MEDIA, f"code of model {spec}/{candidate}")
            if one(services, ARTIFACT_TYPE, row["uid"], head) is None:
                upserts[ARTIFACT_TYPE] = [row]
        upserts[MODEL_TYPE] = [
            {
                "uid": uid,
                "spec": spec,
                "candidate": candidate,
                "family": family if family is not None else entry,
                "code": artifact_uid(artifact) if artifact is not None else None,
                "params": as_json(dict(params)),
                "question": bare(question_uid(question)) if question is not None else None,
                "target": target,
                "features": list(features),
                "reads": None if reads is None else list(reads),
                "interventional": interventional,
                "origin": RUNTIME,
                "status": "candidate",
            }
        ]
        version = commit(services, upserts, note=f"model {spec}/{candidate} registered")
    log.info("model registered", model=uid, family=family or entry, seq=version.seq)
    found = one(services, MODEL_TYPE, uid, version)
    assert found is not None
    return found, version


def retire_model(services: Services, uid: str) -> tuple[dict[str, Any], VersionRef]:
    with _lock:
        head = services.store.head()
        record = model_or_404(services, uid, head)
        if record["origin"] == MANIFEST:
            raise _refuse(
                "model.from_manifest",
                f"{record['uid']} is declared in the manifest; take it out of the manifest "
                "and register again",
            )
        if record["status"] == "retired":
            return record, head
        version = commit(
            services,
            {MODEL_TYPE: [{"uid": record["uid"], "status": "retired"}]},
            note=f"model {record['uid']} retired",
        )
    log.info("model retired", model=record["uid"], seq=version.seq)
    return {**record, "status": "retired"}, version


__all__ = [
    "ACTOR",
    "ARTIFACT_TYPE",
    "BACKTEST_TYPE",
    "FUNCTION_TYPE",
    "LIVE_BRANCH",
    "MODEL_TYPE",
    "QUESTION_TYPE",
    "RUN_TYPE",
    "SCENARIO_TYPE",
    "add_function",
    "add_model",
    "commit",
    "disable_function",
    "register",
    "retire_model",
]
