"""A fingerprint of an ontology's shape, as a person reviewing it would see it."""

from __future__ import annotations

import hashlib
import json

from ..schema import META_CLASS_PREFIX, Ontology

_FORMAT = 1


def type_signature(ontology: Ontology) -> str:
    """sha256 over the domain's object and link types: names, ancestors and the
    classes each link's two ends point at.

    Properties are left out on purpose: adding or changing one keeps the
    signature, while adding, removing or re-parenting a type, or re-pointing a
    link, changes it. Meta-model types are the same in every twin and left out.
    """
    types = []
    for object_type in ontology.domain_types():
        ends = None
        if object_type.link is not None:
            source, target = object_type.link
            ends = [object_type.field(source).target, object_type.field(target).target]
        types.append(
            {"name": object_type.name, "ancestors": list(object_type.ancestors), "link": ends}
        )
    document = {
        "format": _FORMAT,
        "types": sorted(types, key=lambda t: t["name"]),
        "abstract": sorted(
            name for name in ontology.abstract if not name.startswith(META_CLASS_PREFIX)
        ),
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
