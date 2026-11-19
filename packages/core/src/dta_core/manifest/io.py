"""Reading, writing and hashing manifests.

A hash is taken over a canonical JSON form of what the manifest states, so it
does not depend on key order, YAML formatting or comments, nor on whether a
value this software would fill in anyway is written out. A later release that
adds an optional field therefore leaves the hashes of existing manifests, and
the contracts signed under them, as they were. For the same reason the version
of the format a manifest is written in is not part of any hash: it says how the
manifest is written, not what it states.

Reading checks the version before anything else: a manifest newer than this
software is refused as such (`manifest.too_new`) rather than for the fields it
does not know, and one that sets a field newer than the version it names is
refused (`manifest.version_too_low`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from ..errors import ManifestError, ManifestTooNew
from ..log import Log
from .models import MANIFEST_VERSION, Manifest, since_of

log = Log("manifest.io")

VERSION_FIELD = "manifest_version"


def declared_version(data: Any) -> int | None:
    """The format version decoded manifest data names, when it names a whole number."""
    found = data.get(VERSION_FIELD) if isinstance(data, dict) else None
    if isinstance(found, bool):
        return None
    if isinstance(found, int):
        return found
    if isinstance(found, str) and found.strip().isdigit():
        return int(found.strip())
    return None


def _refuse_newer(data: Any, origin: str) -> None:
    declared = declared_version(data)
    if declared is None or declared <= MANIFEST_VERSION:
        return
    log.error(
        "manifest is newer than this software reads",
        origin=origin,
        manifest_version=declared,
        reads=MANIFEST_VERSION,
    )
    raise ManifestTooNew(
        f"{origin} is written in manifest version {declared}; this software reads versions "
        f"up to {MANIFEST_VERSION}: use a newer version of it",
        detail={"origin": origin, "manifest_version": declared, "reads": MANIFEST_VERSION},
    )


def newer_fields(manifest: Manifest) -> list[tuple[str, int]]:
    """Every field the manifest sets that is newer than the version it is read as, with the
    version that introduced it; a field inside one that is reported is not reported again."""
    version = manifest.version

    def walk(node: BaseModel, prefix: str) -> Iterator[tuple[str, int]]:
        for name, info in type(node).model_fields.items():
            value = getattr(node, name)
            if name not in node.model_fields_set or value is None:
                continue
            path = f"{prefix}{name}"
            since = since_of(info)
            if since > version:
                yield path, since
            elif isinstance(value, BaseModel):
                yield from walk(value, f"{path}.")
            elif isinstance(value, dict):
                for key, item in value.items():
                    if isinstance(item, BaseModel):
                        yield from walk(item, f"{path}.{key}.")

    return list(walk(manifest, ""))


def _refuse_too_low(manifest: Manifest, origin: str) -> None:
    found = newer_fields(manifest)
    if not found:
        return
    path, since = found[0]
    needed = max(version for _, version in found)
    more = "" if len(found) == 1 else f" (and {len(found) - 1} more field(s))"
    log.error(
        "manifest sets fields newer than the version it names",
        origin=origin,
        manifest_version=manifest.version,
        fields=[{"path": name, "since": version} for name, version in found],
    )
    raise ManifestError(
        f"{path} needs manifest version {since}, but {origin} names version "
        f"{manifest.version}{more}: set {VERSION_FIELD}: {needed}",
        code="manifest.version_too_low",
        detail={
            "origin": origin,
            "manifest_version": manifest.version,
            "needed": needed,
            "fields": [{"path": name, "since": version} for name, version in found],
        },
    )


def parse_manifest(data: Any, origin: str = "<memory>") -> Manifest:
    """Validate already-decoded data against the schema."""
    if not isinstance(data, dict):
        log.error("manifest is not a mapping", origin=origin, found=type(data).__name__)
        raise ManifestError("manifest must be a mapping", detail={"origin": origin})
    _refuse_newer(data, origin)
    try:
        manifest = Manifest.model_validate(data)
    except ValidationError as exc:
        problems = [
            {"path": ".".join(str(part) for part in err["loc"]), "problem": err["msg"]}
            for err in exc.errors()
        ]
        log.error("manifest does not match the schema", origin=origin, problems=problems)
        raise ManifestError(
            f"{len(problems)} schema problem(s) in {origin}",
            code="manifest.schema",
            detail={"origin": origin, "problems": problems},
        ) from exc
    _refuse_too_low(manifest, origin)
    return manifest


def load_manifest(path: Path) -> Manifest:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.error("manifest cannot be read", exc=exc, path=str(path))
        raise ManifestError(
            f"cannot read {path}", code="manifest.unreadable", detail={"path": str(path)}
        ) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        log.error("manifest is not valid YAML", exc=exc, path=str(path))
        raise ManifestError(
            f"{path} is not valid YAML", code="manifest.syntax", detail={"path": str(path)}
        ) from exc
    manifest = parse_manifest(data, origin=str(path))
    log.debug("manifest loaded", path=str(path), hash=manifest_hash(manifest))
    return manifest


def to_plain(manifest: Manifest) -> dict[str, Any]:
    """JSON-compatible form: durations as ISO 8601, unset fields left out."""
    return manifest.model_dump(mode="json", exclude_none=True)


def dump_manifest(manifest: Manifest) -> str:
    return yaml.safe_dump(to_plain(manifest), sort_keys=False, allow_unicode=True)


def _stated(manifest: Manifest) -> dict[str, Any]:
    """What the manifest says, without the values that are only defaults."""
    return manifest.model_dump(
        mode="json", exclude_none=True, exclude_defaults=True, exclude={VERSION_FIELD}
    )


def _digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def manifest_hash(manifest: Manifest) -> str:
    """Identifies the whole manifest, statuses included."""
    return _digest(_stated(manifest))


def contract_hash(manifest: Manifest) -> str:
    """Identifies what the user signed: the contract and the confirmed ontology types.

    A revision that leaves this hash unchanged stays inside the contract and
    needs no new approval.
    """
    plain = _stated(manifest)
    signature = (plain.get("ontology") or {}).get("type_signature")
    return _digest({"contract": plain.get("contract"), "type_signature": signature})


def access_hash(manifest: Manifest) -> str:
    """Identifies how the sources are reached: a probe holds only while this is unchanged."""
    plain = _stated(manifest)
    sources = {
        source_id: {
            key: (entry or {}).get(key) for key in ("connector", "location", "env", "options")
        }
        for source_id, entry in sorted((plain.get("sources") or {}).items())
    }
    networks = (plain.get("runtime") or {}).get("networks")
    return _digest({"sources": sources, "networks": networks})


def subtree_hash(manifest: Manifest, paths: list[str]) -> str:
    """Hash of the values at the given dotted paths; absent paths hash as null.

    Used to pin a delegated task to exactly the parts of the manifest it reads.
    """
    plain = _stated(manifest)
    picked = {}
    for path in sorted(set(paths)):
        node: Any = plain
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                break
        picked[path] = node
    return _digest(picked)
