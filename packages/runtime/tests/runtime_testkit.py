"""Edits of the water-network ontology used by several tests."""

from __future__ import annotations

_LINED = "      lined:\n        range: boolean\n"


def grow_ontology(text: str) -> str:
    """The same ontology with one more optional property on Pipe."""
    assert _LINED in text
    return text.replace(_LINED, _LINED + "      material:\n        range: string\n")


def shrink_ontology(text: str) -> str:
    """The same ontology without Pipe's `lined` property."""
    assert _LINED in text
    return text.replace(_LINED, "")
