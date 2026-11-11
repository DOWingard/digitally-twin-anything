"""What every handler works with: the twin, its graph cache and version selection.

A request resolves its selector once and passes the resulting version to
every store call it makes, so one response describes one version even while
new versions arrive.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from dta_core.schema import Ontology
from dta_core.store import InvalidInput, Selector, Store, VersionRef

from ..graph import GraphCache
from ..settings import RuntimeSettings
from ..twin import Twin
from .context import note_version
from .problems import ApiError

# A `+` left unencoded in a query string arrives as a space; in a time offset
# it can only have been a plus.
_OFFSET_AFTER_SPACE = re.compile(r"^(\d{4}-\d{2}-\d{2}T[0-9:.]+) (\d{2}(:?\d{2})?)$")

ONTOLOGIES_KEPT = 256


def repair_plus(text: str) -> str:
    """`2026-01-05T00:00:00 01:00` -> `2026-01-05T00:00:00+01:00`."""
    return _OFFSET_AFTER_SPACE.sub(r"\1+\2", text.strip())


def _repair_selector(text: str) -> str:
    return "..".join(repair_plus(part) for part in text.split(".."))


@dataclass
class Services:
    twin: Twin
    settings: RuntimeSettings
    graphs: GraphCache
    _ontologies: OrderedDict[str, Ontology] = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def store(self) -> Store:
        return self.twin.store

    def resolve(self, at: str, branch: str) -> VersionRef:
        """The one version a selector names; a range is refused."""
        text = _repair_selector(at)
        if Selector.parse(text).is_range:
            raise InvalidInput(
                f"one version is needed here, not a range: {at!r}", code="store.bad_selector"
            )
        version = self.store.resolve(text, branch)[0]
        note_version(version)
        return version

    def resolve_cursor(self, payload: Mapping[str, Any]) -> VersionRef:
        """The version a cursor was issued for."""
        version_id, branch = payload.get("v"), payload.get("b")
        if not isinstance(version_id, str) or not isinstance(branch, str):
            raise ApiError("the cursor is not one this API issued", code="api.bad_cursor")
        version = self.store.resolve(version_id, branch)[0]
        note_version(version)
        return version

    def resolve_range(self, selector: str, branch: str) -> tuple[list[VersionRef], str]:
        """The versions a selector names, oldest first, and the same selection
        written with commit ids so that later calls read exactly these versions."""
        text = _repair_selector(selector)
        parsed = Selector.parse(text)
        versions = self.store.resolve(text, branch)
        note_version(versions[-1])
        if not parsed.is_range:
            return versions, versions[0].id
        # Ending a stepped range at its last selected version selects the same versions.
        pinned = f"{versions[0].id}..{versions[-1].id}"
        if parsed.step != 1:
            pinned += f"/{parsed.step}"
        return versions, pinned

    def ontology(self, version: VersionRef) -> Ontology:
        """The ontology in force at a version; a version never changes, so neither does this."""
        with self._lock:
            found = self._ontologies.get(version.id)
            if found is not None:
                self._ontologies.move_to_end(version.id)
                return found
        found = self.store.ontology_at(version)
        with self._lock:
            self._ontologies[version.id] = found
            while len(self._ontologies) > ONTOLOGIES_KEPT:
                self._ontologies.popitem(last=False)
        return found
