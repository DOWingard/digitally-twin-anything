"""Ontologies written in LinkML: loading, convention checks, row validation.

`load_ontology` turns a domain LinkML file into the engine-neutral descriptors
of `dta_core.schema`, with the shared meta-model merged in; `build_validator`
checks rows against it before they are stored; `type_signature` fingerprints
the shape of its types.
"""

from .errors import INVALID, UNREADABLE, OntologyError, Problem
from .expressions import screen_expression
from .loader import META_MODEL_PATH, ParsedOntology, load_ontology, parse_ontology
from .signature import type_signature
from .validator import VALIDATOR_BUILD, OntologyValidator, build_validator, cache_key

__all__ = [
    "INVALID",
    "META_MODEL_PATH",
    "UNREADABLE",
    "VALIDATOR_BUILD",
    "OntologyError",
    "OntologyValidator",
    "ParsedOntology",
    "Problem",
    "build_validator",
    "cache_key",
    "load_ontology",
    "parse_ontology",
    "screen_expression",
    "type_signature",
]
