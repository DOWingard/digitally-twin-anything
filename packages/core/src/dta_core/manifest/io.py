"""Reading, writing and hashing manifests.

The hash is taken over a canonical JSON form, so it does not depend on key
order, YAML formatting or comments.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ..errors import ManifestError
from ..log import Log
from .models import Manifest

log = Log("manifest.io")


def parse_manifest(data: Any, origin: str = "<memory>") -> Manifest:
    """Validate already-decoded data against the schema."""
    if not isinstance(data, dict):
        log.error("manifest is not a mapping", origin=origin, found=type(data).__name__)
        raise ManifestError("manifest must be a mapping", detail={"origin": origin})
    try:
        return Manifest.model_validate(data)
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


def _digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def manifest_hash(manifest: Manifest) -> str:
    """Identifies the whole manifest, statuses included."""
    return _digest(to_plain(manifest))


def contract_hash(manifest: Manifest) -> str:
    """Identifies what the user signed: the contract and the confirmed ontology types.

    A revision that leaves this hash unchanged stays inside the contract and
    needs no new approval.
    """
    plain = to_plain(manifest)
    signature = (plain.get("ontology") or {}).get("type_signature")
    return _digest({"contract": plain.get("contract"), "type_signature": signature})


def subtree_hash(manifest: Manifest, paths: list[str]) -> str:
    """Hash of the values at the given dotted paths; absent paths hash as null.

    Used to pin a delegated task to exactly the parts of the manifest it reads.
    """
    plain = to_plain(manifest)
    picked = {}
    for path in sorted(set(paths)):
        node: Any = plain
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                break
        picked[path] = node
    return _digest(picked)
