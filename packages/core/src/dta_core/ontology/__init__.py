"""Ontologies written in LinkML: loading and convention checks.

`load_ontology` turns a domain LinkML file into the engine-neutral descriptors
of `dta_core.schema`, with the shared meta-model merged in.
"""

from .errors import INVALID, UNREADABLE, OntologyError, Problem
from .expressions import screen_expression
from .loader import META_MODEL_PATH, ParsedOntology, load_ontology, parse_ontology

__all__ = [
    "INVALID",
    "META_MODEL_PATH",
    "UNREADABLE",
    "OntologyError",
    "ParsedOntology",
    "Problem",
    "load_ontology",
    "parse_ontology",
    "screen_expression",
]
