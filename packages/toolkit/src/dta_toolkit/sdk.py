"""The typed Python client of a twin, generated into `<workspace>/sdk/python/`.

Three generators, each from its own source: the types are the Pydantic models
LinkML generates from the twin's ontology (the same generation that validates
the twin's rows), the HTTP client is what openapi-python-client generates from
the OpenAPI document the running twin serves, and the glue between them is
rendered from this package's templates: one accessor per object and link type
and one method per question.

The package is generated again only when what it is made from changed: the
ontology (its type signature or its text), the API's document, the twin's
questions or the generators. Its address and token come from the environment
of whoever uses it; nothing generated holds either.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import keyword
import re
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from importlib import metadata, resources
from pathlib import Path
from typing import Any

from dta_core.errors import DtaError
from dta_core.log import Log
from dta_core.manifest import Manifest
from dta_core.ontology import load_ontology, type_signature
from dta_core.schema import Ontology, snake_case
from jinja2 import Environment, StrictUndefined

from . import addons
from .workspace import Workspace

log = Log("sdk")

MARKER = "sdk.json"
CLIENT_PACKAGE = "client"
TEMPLATES = "templates/sdk"
# Bumped when the templates change what they render, so that a package made by an
# earlier toolkit is generated again.
TEMPLATE_VERSION = 1
# Dependencies of the types and the glue; the HTTP client's come from its generator.
OWN_DEPENDENCIES = ("pydantic>=2.11,<3", "pyarrow>=17")
REQUIRES_PYTHON = ">=3.11"
# Media types the generator does not know, read as text so their operations keep an answer.
CONTENT_TYPE_OVERRIDES = {"application/yaml": "text/plain"}
# What the glue calls, by method and path of the API.
GLUE_OPERATIONS = {
    "list": ("get", "/api/v1/objects/{type}"),
    "one": ("get", "/api/v1/objects/{type}/{uid}"),
    "history": ("get", "/api/v1/objects/{type}/{uid}/history"),
    "links": ("get", "/api/v1/objects/{type}/{uid}/links"),
    "answer": ("post", "/api/v1/questions/{id}/answer"),
    "head": ("get", "/api/v1/meta/health"),
}
GLUE_PARAMETERS = {
    "list": ("type", "at", "branch", "where", "sort", "limit", "cursor", "columns", "format"),
    "one": ("type", "uid", "at", "branch"),
    "history": ("type", "uid", "range", "branch", "properties", "limit", "cursor"),
    "links": ("type", "uid", "at", "branch", "direction", "edge_types", "limit", "cursor"),
    "answer": ("id", "at", "branch"),
    "head": ("branch",),
}
_RESERVED = frozenset({"questions", "close", "base_url", "objects", "head"})
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SdkError(DtaError):
    code = "sdk.failed"


@dataclass
class RenderedSdk:
    package: str
    # Path relative to `sdk/python/` -> content.
    files: dict[str, bytes]
    inputs: dict[str, Any]
    # What the client's generator could not generate, as it said it.
    warnings: list[str] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)


def sdk_dir(workspace: Workspace) -> Path:
    return workspace.root.joinpath(*addons.SDK_DIR)


def marker_path(workspace: Workspace) -> Path:
    return workspace.meta_dir / MARKER


def package_name(twin_id: str) -> str:
    """`water-static` -> `twin_water_static`."""
    return "twin_" + re.sub(r"[^A-Za-z0-9]+", "_", twin_id).strip("_").lower()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def spec_sha(spec: dict[str, Any]) -> str:
    return _sha(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _version(distribution: str) -> str:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return "unknown"


def inputs_of(manifest: Manifest, ontology: Ontology, spec: dict[str, Any]) -> dict[str, Any]:
    """Everything the package is made from; it is generated again when any of it changes."""
    declared = (manifest.contract.questions if manifest.contract is not None else None) or {}
    questions = {qid: (question.text or "") for qid, question in sorted(declared.items())}
    return {
        "twin_id": manifest.twin.id,
        "twin_name": manifest.twin.name,
        "type_signature": type_signature(ontology),
        "ontology_sha256": _sha(ontology.source.encode("utf-8")),
        "api_spec_sha256": spec_sha(spec),
        "questions": questions,
        "base_url_env": manifest.interfaces.base_url_env,
        "token_env": manifest.interfaces.token_env,
        "generators": {
            "linkml": _version("linkml"),
            "openapi-python-client": _version("openapi-python-client"),
            "templates": TEMPLATE_VERSION,
        },
    }


# -- the types -------------------------------------------------------------------


def _types_code(ontology: Ontology) -> str:
    # The generation that validates the twin's rows: the same models for the same file.
    from dta_core.ontology.validator import _generate

    return _generate(ontology)


# -- the HTTP client ---------------------------------------------------------------


def _shape(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


@dataclass(frozen=True)
class GlueOperation:
    tag: str
    module: str
    # API parameter name -> the keyword the generated function takes for it.
    keywords: dict[str, str]
    # API parameter name -> (module, class) of the enum the generated function takes it as.
    enums: dict[str, tuple[str, str]] = field(default_factory=dict)


def _client(
    spec: dict[str, Any], work: Path, project: str
) -> tuple[dict[str, bytes], dict[str, GlueOperation], list[str], list[str]]:
    """The generated client package's files, the operations the glue calls, the client's
    own dependencies and what the generator could not generate."""
    from openapi_python_client import Project
    from openapi_python_client.config import Config, ConfigFile, MetaType
    from openapi_python_client.parser import GeneratorData
    from openapi_python_client.parser.properties import EnumProperty
    from openapi_python_client.strings import PythonIdentifier

    document = work / "openapi.json"
    document.write_text(json.dumps(spec), encoding="utf-8")
    output = work / "generated"
    config = Config.from_sources(
        config_file=ConfigFile(
            project_name_override=project,
            package_name_override=CLIENT_PACKAGE,
            # Formatting would need ruff on the PATH; the code is used as generated.
            post_hooks=[],
            content_type_overrides=dict(CONTENT_TYPE_OVERRIDES),
        ),
        meta_type=MetaType.UV,
        document_source=document,
        file_encoding="utf-8",
        overwrite=True,
        output_path=output,
    )
    parsed = GeneratorData.from_dict(spec, config=config)
    if not isinstance(parsed, GeneratorData):
        log.error("the API's document cannot be read by the client's generator", detail=str(parsed))
        raise SdkError(
            "openapi-python-client cannot read the API's document",
            code="sdk.client_failed",
            detail={"error": str(getattr(parsed, "header", parsed))},
        )
    found: dict[str, GlueOperation] = {}
    for tag, collection in parsed.endpoint_collections_by_tag.items():
        for endpoint in collection.endpoints:
            # The generator renames path parameters (`{type}` becomes `{type_}`).
            key = (endpoint.method.lower(), _shape(endpoint.path.get_untrusted_value()))
            for name, (method, path) in GLUE_OPERATIONS.items():
                if (method, _shape(path)) != key or name in found:
                    continue
                parameters = {
                    prop.name.get_untrusted_value(): str(prop.python_name)
                    for prop in (*endpoint.path_parameters, *endpoint.query_parameters)
                }
                lacking = [p for p in GLUE_PARAMETERS[name] if p not in parameters]
                if lacking:
                    raise SdkError(
                        f"{method.upper()} {path} has no parameter {', '.join(lacking)}",
                        code="sdk.api_unexpected",
                        detail={"operation": [method, path], "lacking": lacking},
                    )
                enums = {
                    prop.name.get_untrusted_value(): (
                        str(prop.class_info.module_name),
                        str(prop.class_info.name),
                    )
                    for prop in endpoint.query_parameters
                    if isinstance(prop, EnumProperty)
                }
                found[name] = GlueOperation(
                    tag=str(tag),
                    module=str(PythonIdentifier(endpoint.name, config.field_prefix)),
                    keywords=parameters,
                    enums=enums,
                )
    absent = sorted(set(GLUE_OPERATIONS) - set(found))
    if absent:
        log.error("the API's document lacks operations the client needs", absent=absent)
        raise SdkError(
            "the API's document lacks "
            + ", ".join(f"{GLUE_OPERATIONS[n][0].upper()} {GLUE_OPERATIONS[n][1]}" for n in absent),
            code="sdk.api_unexpected",
            detail={"absent": [list(GLUE_OPERATIONS[n]) for n in absent]},
        )
    project_ = Project(openapi=parsed, config=config)
    printed = io.StringIO()
    # The generator prints its progress; a command's standard output is its result.
    with contextlib.redirect_stdout(printed):
        errors = project_.build()
    warnings = []
    for error in errors:
        text = " ".join(part for part in (error.header, error.detail) if part)
        level = getattr(getattr(error, "level", None), "value", "error")
        warnings.append(text)
        log.warn("the client's generator reported a problem", level=level, problem=text[:500])
    package_dir = output / CLIENT_PACKAGE
    if not package_dir.is_dir():
        raise SdkError(
            "openapi-python-client produced no package",
            code="sdk.client_failed",
            detail={"errors": warnings},
        )
    for name, operation in found.items():
        if not (package_dir / "api" / operation.tag / f"{operation.module}.py").is_file():
            raise SdkError(
                f"the client has no module for {name}",
                code="sdk.client_failed",
                detail={"operation": name, "module": f"api/{operation.tag}/{operation.module}"},
            )
    meta = tomllib.loads((output / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = list(meta.get("project", {}).get("dependencies") or [])
    files = {
        str(path.relative_to(package_dir)): path.read_bytes()
        for path in sorted(package_dir.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }
    return files, found, dependencies, warnings


# -- the glue ----------------------------------------------------------------------


def _attribute(name: str, taken: set[str]) -> str:
    candidate = snake_case(name)
    if keyword.iskeyword(candidate) or candidate in _RESERVED or candidate in taken:
        candidate += "_objects"
    taken.add(candidate)
    return candidate


def _method(question_id: str, taken: set[str]) -> str:
    candidate = (
        question_id
        if _IDENTIFIER.match(question_id)
        else snake_case(re.sub(r"[^A-Za-z0-9_]+", "_", question_id)).strip("_")
    )
    if not candidate or candidate[0].isdigit():
        candidate = f"q_{candidate}"
    if keyword.iskeyword(candidate) or candidate in {"ask"} | taken:
        candidate += "_question"
    taken.add(candidate)
    return candidate


def _render(name: str, context: dict[str, Any]) -> bytes:
    text = resources.files("dta_toolkit").joinpath(f"{TEMPLATES}/{name}").read_text("utf-8")
    environment = Environment(
        undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True
    )
    return environment.from_string(text).render(**context).encode("utf-8")


def render_sdk(manifest: Manifest, ontology: Ontology, spec: dict[str, Any]) -> RenderedSdk:
    """Every file of the package, rendered in a directory of its own and read back."""
    twin_id = manifest.twin.id
    package = package_name(twin_id)
    inputs = inputs_of(manifest, ontology, spec)
    timings: dict[str, float] = {}
    started = time.perf_counter()
    types_code = _types_code(ontology)
    timings["types_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="dta-sdk-") as scratch:
        client_files, operations, client_deps, warnings = _client(
            spec, Path(scratch), f"{package.replace('_', '-')}-client"
        )
    timings["client_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    taken: set[str] = set()
    types = [
        {
            "name": t.name,
            "attribute": _attribute(t.name, taken),
            "link": t.link is not None,
            "description": t.description,
        }
        for t in ontology.domain_types()
    ]
    asked: set[str] = set()
    questions = [
        {"id": qid, "method": _method(qid, asked), "text": text}
        for qid, text in inputs["questions"].items()
    ]
    digest = _sha(json.dumps(inputs, sort_keys=True).encode("utf-8"))[:12]
    context = {
        "package": package,
        "distribution": f"twin-{re.sub(r'[^A-Za-z0-9]+', '-', twin_id).strip('-').lower()}",
        "version": f"0.1.0+g{digest}",
        "twin_id": twin_id,
        "twin_name": manifest.twin.name or twin_id,
        "types": types,
        "questions": questions,
        "ops": {
            name: {
                "tag": op.tag,
                "module": op.module,
                "kw": op.keywords,
                "enums": {key: {"module": m, "name": c} for key, (m, c) in op.enums.items()},
            }
            for name, op in operations.items()
        },
        "base_url_env": inputs["base_url_env"],
        "token_env": inputs["token_env"],
        "dependencies": [*client_deps, *OWN_DEPENDENCIES],
        "requires_python": REQUIRES_PYTHON,
        "client_package": CLIENT_PACKAGE,
        "example_type": types[0] if types else None,
        "example_question": questions[0] if questions else None,
        "json": json,
    }
    started = time.perf_counter()
    files: dict[str, bytes] = {
        "pyproject.toml": _render("pyproject.toml.j2", context),
        "README.md": _render("README.md.j2", context),
        f"{package}/__init__.py": _render("__init__.py.j2", context),
        f"{package}/twin.py": _render("twin.py.j2", context),
        f"{package}/py.typed": b"",
        f"{package}/types.py": types_code.encode("utf-8"),
    }
    for relative, data in client_files.items():
        files[f"{package}/{CLIENT_PACKAGE}/{relative}"] = data
    timings["glue_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    log.info(
        "client package rendered",
        package=package,
        files=len(files),
        types=len(types),
        questions=len(questions),
        warnings=len(warnings),
        **timings,
    )
    return RenderedSdk(
        package=package, files=files, inputs=inputs, warnings=warnings, timings_ms=timings
    )


# -- on disk -----------------------------------------------------------------------


def _marker(workspace: Workspace) -> dict[str, Any] | None:
    path = marker_path(workspace)
    if not path.is_file():
        return None
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warn(
            "the client package's record cannot be read; it is generated again",
            error=type(exc).__name__,
        )
        return None
    return found if isinstance(found, dict) else None


def current(workspace: Workspace, inputs: dict[str, Any]) -> bool:
    """Whether the package on disk was generated from these inputs and is untouched."""
    found = _marker(workspace)
    if found is None or found.get("inputs") != inputs:
        return False
    root = sdk_dir(workspace)
    hashes = found.get("files") or {}
    for relative, digest in hashes.items():
        path = root / relative
        if not path.is_file() or _sha(path.read_bytes()) != digest:
            log.info("a file of the client package changed; it is generated again", file=relative)
            return False
    present = (
        {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
        if root.is_dir()
        else set()
    )
    return present == set(hashes)


def write_sdk(workspace: Workspace, rendered: RenderedSdk) -> dict[str, Any]:
    """Bring `sdk/python/` in line with the rendered package and record what it was made from."""
    root = sdk_dir(workspace)
    written: list[str] = []
    removed: list[str] = []
    with workspace.exclusive():
        for relative, data in sorted(rendered.files.items()):
            target = root / relative
            if target.is_file() and target.read_bytes() == data:
                continue
            workspace.atomic_write(target, data)
            written.append(relative)
        produced = {root / relative for relative in rendered.files}
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_dir() and not path.is_symlink():
                with contextlib.suppress(OSError):
                    path.rmdir()  # only once it is empty
            elif path not in produced:
                workspace.remove(path)
                removed.append(str(path.relative_to(root)))
        record = {
            "package": rendered.package,
            "inputs": rendered.inputs,
            "files": {rel: _sha(data) for rel, data in sorted(rendered.files.items())},
            "warnings": rendered.warnings,
        }
        workspace.atomic_write(marker_path(workspace), json.dumps(record, indent=2) + "\n")
    log.info("client package written", path=str(root), written=len(written), removed=len(removed))
    return {"written": len(written), "removed": removed}


def remove_sdk(workspace: Workspace) -> list[str]:
    """Remove the generated package and its record; what was removed, relative to `sdk/python/`."""
    root = sdk_dir(workspace)
    removed: list[str] = []
    with workspace.exclusive():
        if root.is_dir():
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_dir() and not path.is_symlink():
                    with contextlib.suppress(OSError):
                        path.rmdir()
                else:
                    workspace.remove(path)
                    removed.append(str(path.relative_to(root)))
            for directory in (root, root.parent):
                with contextlib.suppress(OSError):
                    directory.rmdir()
        if marker_path(workspace).is_file():
            workspace.remove(marker_path(workspace))
    if removed:
        log.info("client package removed: the add-on is off", files=len(removed))
    return removed


def ensure_sdk(
    workspace: Workspace, manifest: Manifest, ontology_file: Path, spec: dict[str, Any]
) -> dict[str, Any]:
    """Generate the package when what it is made from changed; say what was done."""
    started = time.perf_counter()
    ontology = load_ontology(ontology_file)
    inputs = inputs_of(manifest, ontology, spec)
    root = sdk_dir(workspace)
    package = package_name(manifest.twin.id)
    if current(workspace, inputs):
        log.info("client package is current", package=package)
        return {
            "action": "current",
            "path": str(root),
            "package": package,
            "type_signature": inputs["type_signature"],
            "api_spec_sha256": inputs["api_spec_sha256"],
        }
    rendered = render_sdk(manifest, ontology, spec)
    result = write_sdk(workspace, rendered)
    elapsed = round((time.perf_counter() - started) * 1000.0, 1)
    log.info("client package generated", package=package, duration_ms=elapsed)
    return {
        "action": "generated",
        "path": str(root),
        "package": package,
        "files": len(rendered.files),
        **result,
        "type_signature": inputs["type_signature"],
        "api_spec_sha256": inputs["api_spec_sha256"],
        "warnings": rendered.warnings,
        "timings_ms": {**rendered.timings_ms, "total_ms": elapsed},
    }
