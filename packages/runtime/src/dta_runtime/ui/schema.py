"""The twin's ontology at one version, as `/meta/schema` describes it.

Everything the interface knows about a twin's domain comes from here: which
types exist, which are links, their properties, kinds and units. Nothing is
named in code, so the same interface serves any twin.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Distinguishable on white and for most colour-vision deficiencies (Okabe-Ito,
# then Tableau 10); types beyond these reuse colours in order.
PALETTE = (
    "#0072B2",
    "#E69F00",
    "#009E73",
    "#CC79A7",
    "#D55E00",
    "#56B4E9",
    "#F0E442",
    "#000000",
    "#4E79A7",
    "#F28E2B",
    "#E15759",
    "#76B7B2",
    "#59A14F",
    "#EDC948",
    "#B07AA1",
    "#FF9DA7",
    "#9C755F",
    "#BAB0AC",
)
NUMERIC_KINDS = ("integer", "float")
# Kinds held as lists or structured values, which the API neither filters nor sorts on.
STRUCTURED_KIND = "json"


@dataclass(frozen=True)
class Schema:
    version_id: str
    types: dict[str, dict[str, Any]]
    edge_types: tuple[dict[str, Any], ...]

    @classmethod
    def from_document(cls, version_id: str, document: dict[str, Any]) -> Schema:
        types = {entry["name"]: entry for entry in document.get("types", [])}
        return cls(version_id, types, tuple(document.get("edge_types", [])))

    def has(self, name: str | None) -> bool:
        return name is not None and name in self.types

    def is_link(self, name: str) -> bool:
        return self.types[name]["kind"] == "link"

    def is_meta(self, name: str) -> bool:
        return bool(self.types[name].get("meta"))

    def node_types(self) -> list[str]:
        """Types whose objects are nodes of the graph: the domain's first, then the twin's own."""
        names = [name for name in self.types if not self.is_link(name)]
        return sorted(names, key=lambda name: (self.is_meta(name), name))

    def sheet_types(self) -> list[str]:
        """Every concrete type: domain objects, then links, then the twin's own records."""
        return sorted(self.types, key=lambda name: (self.is_meta(name), self.is_link(name), name))

    def default_node_type(self) -> str | None:
        """The first domain type that is not an event, else the first node type at all."""
        names = self.node_types()
        for name in names:
            if not self.is_meta(name) and not self.types[name].get("event"):
                return name
        return names[0] if names else None

    def properties(self, name: str) -> list[dict[str, Any]]:
        return list(self.types[name]["properties"])

    def property(self, name: str, prop: str) -> dict[str, Any] | None:
        if name not in self.types:
            return None
        for entry in self.types[name]["properties"]:
            if entry["name"] == prop:
                return entry
        return None

    def unit(self, name: str, prop: str) -> str | None:
        found = self.property(name, prop)
        return found.get("unit") if found else None

    def colour(self, name: str | None) -> str:
        names = sorted(self.node_types())
        if name not in names:
            return "#888888"
        return PALETTE[names.index(name) % len(PALETTE)]

    def type_label(self, name: str) -> str:
        if self.is_meta(name):
            return f"{name} (twin record)"
        if self.is_link(name):
            return f"{name} (link)"
        return name


def filterable(prop: dict[str, Any]) -> bool:
    return prop["kind"] != STRUCTURED_KIND and not prop.get("multivalued")
