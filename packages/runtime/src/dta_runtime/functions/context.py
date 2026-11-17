"""What a built-in is given to work with."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dta_core.schema import Ontology
    from dta_core.store import Store, VersionRef

    from ..graph import GraphCache
    from ..protocol import Answer


@dataclass(frozen=True)
class Context:
    store: Store
    # As of `version`.
    ontology: Ontology
    # The pinned version every read is made at. A range a call names ends at or before
    # it, and `head` in a call means this version: nothing committed after it is seen.
    version: VersionRef
    graphs: GraphCache
    # `time.monotonic()` value after which the run gives up.
    deadline: float
    # What computes a forecast: the resolved parameters and this context in, the
    # answer out. None where no model can be run.
    forecast: Callable[[Mapping[str, Any], Context], Answer] | None = None
