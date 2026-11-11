"""Engine-neutral view of a LinkML schema, plus translation into each engine's schema.

The benchmark answers whether one LinkML file can drive every store, so no
engine schema is written by hand: both are derived from the same model here.
"""

from __future__ import annotations

from dataclasses import dataclass
from graphlib import TopologicalSorter
from pathlib import Path

from linkml_runtime.utils.schemaview import SchemaView

from .log import Log

log = Log("ontology")

_FLOAT_RANGES = {"float", "double", "decimal"}
_INTEGER_RANGES = {"integer"}


@dataclass(frozen=True)
class Field:
    name: str
    kind: str  # "string" | "float" | "integer" | "enum" | "ref"
    required: bool
    identifier: bool
    target: str | None  # referenced type for "ref", enum name for "enum"
    minimum: float | None


@dataclass(frozen=True)
class ObjectType:
    name: str
    fields: tuple[Field, ...]
    link: tuple[str, str] | None  # (source field, target field) when the type is a link

    @property
    def table(self) -> str:
        return self.name.lower()

    def field(self, name: str) -> Field:
        for candidate in self.fields:
            if candidate.name == name:
                return candidate
        raise KeyError(f"{self.name} has no field {name}")


@dataclass(frozen=True)
class Ontology:
    types: tuple[ObjectType, ...]  # referenced types precede the types that reference them
    enums: dict[str, tuple[str, ...]]

    def type(self, name: str) -> ObjectType:
        for candidate in self.types:
            if candidate.name == name:
                return candidate
        raise KeyError(f"unknown type {name}")


def load_ontology(path: Path) -> Ontology:
    """Read a LinkML schema and order its classes so references resolve on insert."""
    try:
        view = SchemaView(str(path))
        class_names = set(view.all_classes().keys())
        enum_names = set(view.all_enums().keys())
        enums = {
            str(name): tuple(str(value) for value in enum.permissible_values)
            for name, enum in view.all_enums().items()
        }
        types: dict[str, ObjectType] = {}
        for class_name in class_names:
            fields = []
            for slot in view.class_induced_slots(class_name):
                slot_range = str(slot.range)
                if slot_range in enum_names:
                    kind, target = "enum", slot_range
                elif slot_range in class_names:
                    kind, target = "ref", slot_range
                elif slot_range in _FLOAT_RANGES:
                    kind, target = "float", None
                elif slot_range in _INTEGER_RANGES:
                    kind, target = "integer", None
                else:
                    kind, target = "string", None
                fields.append(
                    Field(
                        name=str(slot.name),
                        kind=kind,
                        required=bool(slot.required),
                        identifier=bool(slot.identifier),
                        target=target,
                        minimum=float(slot.minimum_value)
                        if slot.minimum_value is not None
                        else None,
                    )
                )
            annotations = {
                str(tag): getattr(value, "value", value)
                for tag, value in dict(view.get_class(class_name).annotations or {}).items()
            }
            link = None
            if "link_source" in annotations and "link_target" in annotations:
                link = (str(annotations["link_source"]), str(annotations["link_target"]))
            types[str(class_name)] = ObjectType(str(class_name), tuple(fields), link)
    except Exception as exc:
        log.error("failed to load LinkML schema", exc=exc, path=str(path))
        raise

    dependencies = {
        name: {f.target for f in t.fields if f.kind == "ref" and f.target != name}
        for name, t in types.items()
    }
    ordered = tuple(types[name] for name in TopologicalSorter(dependencies).static_order())
    log.info(
        "ontology loaded",
        path=str(path),
        types=[t.name for t in ordered],
        enums=sorted(enums),
    )
    return Ontology(types=ordered, enums=enums)


def to_sql_ddl(ontology: Ontology) -> list[str]:
    """One CREATE TABLE per type, with keys, enums, foreign keys and range checks."""
    statements = []
    for object_type in ontology.types:
        columns, constraints = [], []
        for field in object_type.fields:
            if field.identifier:
                sql_type = "VARCHAR(64)"
            elif field.kind == "float":
                sql_type = "DOUBLE"
            elif field.kind == "integer":
                sql_type = "BIGINT"
            elif field.kind == "enum":
                values = ", ".join(f"'{value}'" for value in ontology.enums[field.target])
                sql_type = f"ENUM({values})"
            elif field.kind == "ref":
                sql_type = "VARCHAR(64)"
                constraints.append(
                    f"FOREIGN KEY (`{field.name}`) REFERENCES `{field.target.lower()}` (`uid`)"
                )
            else:
                sql_type = "VARCHAR(255)"
            null = " NOT NULL" if field.required or field.identifier else ""
            key = " PRIMARY KEY" if field.identifier else ""
            columns.append(f"`{field.name}` {sql_type}{null}{key}")
            if field.minimum is not None:
                constraints.append(f"CHECK (`{field.name}` >= {field.minimum})")
        body = ",\n  ".join(columns + constraints)
        statements.append(f"CREATE TABLE `{object_type.table}` (\n  {body}\n)")
    return statements


def to_terminus_schema(ontology: Ontology) -> tuple[list[dict], list[str]]:
    """Schema documents for TerminusDB, and the constraints it cannot express."""
    documents: list[dict] = [
        {"@type": "Enum", "@id": name, "@value": list(values)}
        for name, values in ontology.enums.items()
    ]
    unenforced: list[str] = []
    for object_type in ontology.types:
        document: dict = {"@type": "Class", "@id": object_type.name}
        for field in object_type.fields:
            if field.identifier:
                document["@key"] = {"@type": "Lexical", "@fields": [field.name]}
                value_type: str = "xsd:string"
            elif field.kind == "float":
                value_type = "xsd:double"
            elif field.kind == "integer":
                value_type = "xsd:integer"
            elif field.kind in ("enum", "ref"):
                value_type = field.target
            else:
                value_type = "xsd:string"
            document[field.name] = (
                value_type
                if field.required or field.identifier
                else {"@type": "Optional", "@class": value_type}
            )
            if field.minimum is not None:
                unenforced.append(f"{object_type.name}.{field.name} >= {field.minimum}")
        documents.append(document)
    return documents, unenforced
