"""Checking rows against the ontology before they are written.

The checks are Pydantic models produced by LinkML's own generator from the
merged schema, so the rules applied here are exactly LinkML's reading of the
file. Generated code is cached on disk under a key derived from the ontology
and the generator's version: building a validator for an ontology seen before
costs an import, not a generation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import types
import warnings
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path
from typing import Any

from linkml_runtime.linkml_model.meta import SchemaDefinition
from pydantic import BaseModel, TypeAdapter, ValidationError

from ..log import Log
from ..schema import UID_FIELD, UID_MAX_LENGTH, ObjectType, Ontology, Violation
from .errors import OntologyError
from .loader import meta_model_text, parse_ontology

log = Log("ontology.validator")

VALIDATOR_BUILD = "ontology.validator"
_FORMAT = 1
_PACKAGE_DIR = Path(__file__).resolve().parent.parent
_HEADER = re.compile(r"^# dta-ontology-models key=([0-9a-f]{64}) sha256=([0-9a-f]{64})\n")
# Pydantic error types and the rule each one reports.
_RULES = {
    "missing": "required",
    "extra_forbidden": "unknown_property",
    "enum": "enum",
    "literal_error": "enum",
    "greater_than": "range",
    "greater_than_equal": "range",
    "less_than": "range",
    "less_than_equal": "range",
    "string_pattern_mismatch": "pattern",
}
# LinkML's generator enforces `pattern` in a validator that raises with this message.
_PATTERN_MESSAGE = re.compile(r"^Value error, Invalid \S+ format: ")
# Models built in this process, by cache key.
_MODULES: dict[str, types.ModuleType] = {}


class OntologyValidator:
    """Row validator for one ontology; satisfies `dta_core.schema.RowValidator`."""

    def __init__(self, ontology: Ontology, models: types.ModuleType) -> None:
        self.ontology = ontology
        self._models = models
        self._types = {t.name: t for t in ontology.types}
        self._adapters: dict[str, TypeAdapter] = {}

    def model(self, type_name: str) -> type[BaseModel]:
        """The generated Pydantic model of a concrete type."""
        if type_name not in self._types:
            raise KeyError(f"unknown type {type_name}")
        return getattr(self._models, type_name)

    def _adapter(self, type_name: str) -> TypeAdapter:
        adapter = self._adapters.get(type_name)
        if adapter is None:
            adapter = TypeAdapter(list[self.model(type_name)])  # type: ignore[misc]
            self._adapters[type_name] = adapter
        return adapter

    def validate(
        self, type_name: str, rows: list[dict[str, Any]], partial: bool = False
    ) -> list[Violation]:
        """Every violation in `rows`, in row order; never raises for bad data.

        With `partial` true only the properties present in each row are checked,
        so a row may omit required properties.
        """
        started = time.perf_counter()
        try:
            rows = list(rows)
        except TypeError as exc:
            log.warn("rows are not iterable", type=type_name, error=str(exc))
            return [Violation(type_name, None, None, "type", "rows must be a list of mappings")]
        object_type = self._types.get(type_name)
        if object_type is None:
            log.warn("validation requested for an unknown type", type=type_name, rows=len(rows))
            return [
                Violation(
                    type_name,
                    _uid_of(row),
                    None,
                    "unknown_type",
                    f"{type_name} is not a concrete type of the ontology",
                )
                for row in rows
            ]

        found: list[tuple[int, Violation]] = []
        try:
            self._adapter(type_name).validate_python(rows)
        except ValidationError as exc:
            found.extend(self._translate(object_type, rows, exc.errors(include_url=False), partial))
        except Exception as exc:  # a generated validator failing on odd input
            log.error(
                "batch validation failed unexpectedly; validating row by row",
                exc=exc,
                type=type_name,
                rows=len(rows),
            )
            found.extend(self._row_by_row(object_type, rows, partial))
        found.extend(self._check_uids(object_type, rows))
        found.sort(key=lambda item: item[0])
        violations = [violation for _, violation in found]
        log.debug(
            "rows validated",
            type=type_name,
            rows=len(rows),
            partial=partial,
            violations=len(violations),
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return violations

    # -- translation of Pydantic errors

    def _translate(
        self,
        object_type: ObjectType,
        rows: list[Any],
        errors: Iterable[dict[str, Any]],
        partial: bool,
        offset: int | None = None,
    ) -> list[tuple[int, Violation]]:
        result = []
        for error in errors:
            loc = tuple(error.get("loc", ()))
            if offset is None:
                index = loc[0] if loc and isinstance(loc[0], int) else -1
                inner = loc[1:]
            else:
                index, inner = offset, loc
            row = rows[index] if 0 <= index < len(rows) else None
            prop = str(inner[0]) if inner and isinstance(inner[0], str) else None
            kind = error.get("type", "")
            if partial and kind == "missing" and len(inner) == 1:
                continue
            rule = self._rule(object_type, prop, inner, error)
            if not isinstance(row, dict):
                message = "a row must be a mapping of property names to values"
            else:
                path = _path(inner)
                message = f"{path}: {error.get('msg', '')}" if path else str(error.get("msg", ""))
            result.append((index, Violation(object_type.name, _uid_of(row), prop, rule, message)))
        return result

    def _rule(
        self, object_type: ObjectType, prop: str | None, inner: tuple, error: dict[str, Any]
    ) -> str:
        kind = error.get("type", "")
        if kind in _RULES:
            return _RULES[kind]
        if kind == "value_error":
            top_level_pattern = (
                len(inner) >= 1
                and prop is not None
                and object_type.has_field(prop)
                and object_type.field(prop).pattern is not None
            )
            if top_level_pattern or _PATTERN_MESSAGE.match(str(error.get("msg", ""))):
                return "pattern"
        if len(inner) == 1 and prop is not None and error.get("input", ...) is None:
            if prop == UID_FIELD or (
                object_type.has_field(prop) and object_type.field(prop).required
            ):
                return "required"
        return "type"

    def _row_by_row(
        self, object_type: ObjectType, rows: list[Any], partial: bool
    ) -> list[tuple[int, Violation]]:
        model = self.model(object_type.name)
        result: list[tuple[int, Violation]] = []
        for index, row in enumerate(rows):
            try:
                model.model_validate(row)
            except ValidationError as exc:
                result.extend(
                    self._translate(
                        object_type, rows, exc.errors(include_url=False), partial, offset=index
                    )
                )
            except Exception as exc:
                log.error("row could not be validated", exc=exc, type=object_type.name, index=index)
                result.append(
                    (
                        index,
                        Violation(
                            object_type.name,
                            _uid_of(row),
                            None,
                            "type",
                            f"row could not be validated: {type(exc).__name__}: {exc}",
                        ),
                    )
                )
        return result

    def _check_uids(self, object_type: ObjectType, rows: list[Any]) -> list[tuple[int, Violation]]:
        """A uid present in a row must be `<scope>:<key>` and fit the uid column."""
        prefix = object_type.scope + ":"
        result = []
        for index, row in enumerate(rows):
            uid = row.get(UID_FIELD) if isinstance(row, dict) else None
            if not isinstance(uid, str):
                continue  # absent or mistyped: reported by the model
            problem = None
            if not uid.startswith(prefix) or len(uid) == len(prefix):
                problem = f"uid must have the form {prefix}<key>"
            elif len(uid) > UID_MAX_LENGTH:
                problem = f"uid is longer than {UID_MAX_LENGTH} characters"
            if problem:
                result.append((index, Violation(object_type.name, uid, UID_FIELD, "uid", problem)))
        return result


def _uid_of(row: Any) -> str | None:
    if isinstance(row, dict):
        uid = row.get(UID_FIELD)
        if isinstance(uid, str):
            return uid
    return None


def _path(loc: tuple) -> str:
    text = ""
    for part in loc:
        text += f"[{part}]" if isinstance(part, int) else (f".{part}" if text else str(part))
    return text


# -- building -------------------------------------------------------------------


def build_validator(ontology: Ontology, cache_dir: str | Path | None = None) -> OntologyValidator:
    """A validator for every concrete type of `ontology`.

    Models are generated from the ontology's LinkML source merged with the
    meta-model, and cached in `cache_dir` (default: a private directory under
    the system temp directory).
    """
    started = time.perf_counter()
    key = cache_key(ontology)
    module = _MODULES.get(key)
    origin = "memory"
    if module is None:
        module, origin = _load_or_generate(ontology, key, cache_dir)
        _MODULES[key] = module
    missing = [
        t.name
        for t in ontology.types
        if not (
            isinstance(getattr(module, t.name, None), type)
            and issubclass(getattr(module, t.name), BaseModel)
        )
    ]
    if missing:
        log.error("generated models lack some types", missing=missing, key=key)
        raise OntologyError(
            f"generated models lack {len(missing)} type(s)",
            code=VALIDATOR_BUILD,
            detail={"missing": missing},
        )
    log.info(
        "validator built",
        ontology=ontology.hash,
        key=key,
        source=origin,
        types=len(ontology.types),
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return OntologyValidator(ontology, module)


def cache_key(ontology: Ontology) -> str:
    """Changes with the ontology, the meta-model and the generator's version."""
    document = {
        "format": _FORMAT,
        "ontology": ontology.hash,
        "meta": hashlib.sha256(meta_model_text().encode("utf-8")).hexdigest(),
        "linkml": _version("linkml"),
        "pydantic": _version("pydantic"),
    }
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()


def _version(distribution: str) -> str:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        log.warn("package version unknown; cache key uses a placeholder", package=distribution)
        return "unknown"


def _load_or_generate(
    ontology: Ontology, key: str, cache_dir: str | Path | None
) -> tuple[types.ModuleType, str]:
    directory = _cache_directory(cache_dir)
    path = directory / f"models_{key}.py" if directory is not None else None
    if path is not None and path.exists():
        code = _read_cached(path, key)
        if code is not None:
            try:
                module = _execute(code, key, str(path))
                log.debug("models loaded from cache", path=str(path))
                return module, "cache"
            except Exception as exc:
                log.warn("cached models do not load; regenerating", path=str(path), error=str(exc))
    code = _generate(ontology)
    if path is not None:
        _write_cached(path, key, code)
    return _execute(code, key, str(path) if path is not None else "<generated models>"), "generated"


def _generate(ontology: Ontology) -> str:
    started = time.perf_counter()
    parsed = parse_ontology(ontology.source, origin="<ontology source>")
    if parsed.ontology.to_json() != ontology.to_json():
        log.warn(
            "descriptors differ from the ontology's LinkML source; models follow the source",
            given=ontology.hash,
            from_source=parsed.ontology.hash,
        )
    # Imported here: the generator package is heavy and only needed on a cache miss.
    from linkml.generators.pydanticgen import PydanticGenerator

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            schema = SchemaDefinition(**copy.deepcopy(parsed.schema))
            code = PydanticGenerator(schema, metadata_mode=None).serialize()
    except Exception as exc:
        log.error("LinkML could not generate models", exc=exc, ontology=ontology.hash)
        raise OntologyError(
            "LinkML could not generate validation models",
            code=VALIDATOR_BUILD,
            detail={"reason": f"{type(exc).__name__}: {exc}"},
        ) from exc
    log.info(
        "validation models generated",
        ontology=ontology.hash,
        bytes=len(code),
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return code


def _execute(code: str, key: str, origin: str) -> types.ModuleType:
    name = f"dta_ontology_models_{key[:16]}"
    module = types.ModuleType(name)
    module.__file__ = origin
    # Pydantic resolves forward references through sys.modules.
    sys.modules[name] = module
    try:
        exec(compile(code, origin, "exec"), module.__dict__)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def _default_cache_root() -> Path:
    owner = os.getuid() if hasattr(os, "getuid") else "user"
    return Path(tempfile.gettempdir()) / f"dta-ontology-models-{owner}"


def _cache_directory(cache_dir: str | Path | None) -> Path | None:
    """A directory safe to execute cached code from, or None to skip caching."""
    directory = Path(cache_dir) if cache_dir is not None else _default_cache_root()
    if directory.resolve().is_relative_to(_PACKAGE_DIR):
        log.warn(
            "cache directory inside the package refused; using the default",
            requested=str(directory),
        )
        directory = _default_cache_root()
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        log.warn(
            "cache directory unavailable; models will not be cached",
            path=str(directory),
            error=str(exc),
        )
        return None
    if hasattr(os, "getuid"):
        status = directory.stat()
        if status.st_uid != os.getuid() or status.st_mode & 0o022:
            log.warn(
                "cache directory is writable by others; models will not be cached",
                path=str(directory),
            )
            return None
    return directory


def _read_cached(path: Path, key: str) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warn("cached models cannot be read; regenerating", path=str(path), error=str(exc))
        return None
    header = _HEADER.match(text)
    body = text[header.end() :] if header else ""
    if (
        header is None
        or header.group(1) != key
        or header.group(2) != hashlib.sha256(body.encode("utf-8")).hexdigest()
    ):
        log.warn("cached models are damaged; regenerating", path=str(path))
        return None
    return body


def _write_cached(path: Path, key: str, code: str) -> None:
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    text = f"# dta-ontology-models key={key} sha256={digest}\n{code}"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
        log.debug("models cached", path=str(path))
    except OSError as exc:
        log.warn("models could not be cached", path=str(path), error=str(exc))
        temporary.unlink(missing_ok=True)
