"""Helpers of the runtime tests that are imported by name: the water network's
manifest and edits of its ontology."""

from __future__ import annotations

import yaml
from dta_core.manifest import Manifest, parse_manifest
from dta_core.ontology import parse_ontology, type_signature

_LINED = "      lined:\n        range: boolean\n"


def water_manifest(ontology_text: str, **changes: object) -> Manifest:
    """A small manifest for the water network; `changes` replace top-level sections."""
    document: dict[str, object] = {
        "twin": {"id": "water", "name": "Water network", "revision": 1},
        "contract": {"tier": "static"},
        "ontology": {
            "path": "ontology/water_network.yaml",
            "type_signature": type_signature(parse_ontology(ontology_text).ontology),
        },
    }
    document.update(changes)
    return parse_manifest(yaml.safe_load(yaml.safe_dump(document)), origin="test manifest")


def grow_ontology(text: str) -> str:
    """The same ontology with one more optional property on Pipe."""
    assert _LINED in text
    return text.replace(_LINED, _LINED + "      material:\n        range: string\n")


def shrink_ontology(text: str) -> str:
    """The same ontology without Pipe's `lined` property."""
    assert _LINED in text
    return text.replace(_LINED, "")
