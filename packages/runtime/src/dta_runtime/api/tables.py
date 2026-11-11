"""Arrow tables as the API hands them out, and the cursors that page through them.

Every tabular answer is built once as an Arrow table; JSON, Arrow and Parquet
are renderings of it. Columns holding JSON text (structured values and
provenance) are marked in their field metadata, so that the JSON rendering
turns them into JSON values and an Arrow client can tell them apart.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

import pyarrow as pa
from dta_core.schema import Kind, ObjectType
from dta_core.store import VersionRef

from .problems import ApiError

JSON_FIELD = b"dta:json"
VERSION_KEY = b"dta:version"
PROV_COLUMN = "_prov"
TYPE_COLUMN = "_type"


def iso(value: Any) -> str:
    """ISO 8601 in UTC, `Z` for the offset."""
    return value.isoformat().replace("+00:00", "Z")


def version_json(version: VersionRef) -> dict[str, Any]:
    return {"id": version.id, "seq": version.seq, "t": iso(version.t), "branch": version.branch}


def version_headers(version: VersionRef) -> dict[str, str]:
    return {
        "x-dta-version-id": version.id,
        "x-dta-version-seq": str(version.seq),
        "x-dta-version-t": iso(version.t),
        "x-dta-version-branch": version.branch,
    }


def is_json_field(field: pa.Field) -> bool:
    return bool(field.metadata) and field.metadata.get(JSON_FIELD) == b"true"


def mark_json(table: pa.Table, names: Iterable[str]) -> pa.Table:
    wanted = set(names)
    if not wanted.intersection(table.column_names):
        return table
    fields = [
        field.with_metadata({**(field.metadata or {}), JSON_FIELD: b"true"})
        if field.name in wanted
        else field
        for field in table.schema
    ]
    schema = pa.schema(fields, metadata=table.schema.metadata)
    return pa.Table.from_arrays(table.columns, schema=schema)


def json_properties(object_type: ObjectType) -> list[str]:
    return [field.name for field in object_type.fields if field.kind == Kind.json]


def _expand_provenance(text: str) -> str:
    entries = json.loads(text)
    return json.dumps(
        {
            name: {
                "source": entry["s"],
                "kind": entry["k"],
                "confidence": entry["c"],
                "t_valid": entry["t"],
            }
            for name, entry in entries.items()
        },
        separators=(",", ":"),
    )


def provenance_json(provenance: Mapping[str, Any]) -> dict[str, Any]:
    """Provenance of a stored object (`Provenance` values by property) as JSON."""
    return {
        name: {
            "source": entry.source,
            "kind": entry.kind.value,
            "confidence": entry.confidence,
            "t_valid": iso(entry.t_valid),
        }
        for name, entry in provenance.items()
    }


def readable(table: pa.Table, object_type: ObjectType | None) -> pa.Table:
    """Mark JSON columns and spell out the stored provenance's short keys."""
    if PROV_COLUMN in table.column_names:
        position = table.column_names.index(PROV_COLUMN)
        values = table.column(PROV_COLUMN).to_pylist()
        expanded = pa.array(
            [None if value is None else _expand_provenance(value) for value in values], pa.string()
        )
        table = table.set_column(position, table.schema.field(position), expanded)
    names = [PROV_COLUMN]
    if object_type is not None:
        names.extend(json_properties(object_type))
    return mark_json(table, names)


def with_version(table: pa.Table, version: VersionRef) -> pa.Table:
    metadata = dict(table.schema.metadata or {})
    metadata[VERSION_KEY] = json.dumps(version_json(version)).encode("utf-8")
    return table.replace_schema_metadata(metadata)


# -- cursors -------------------------------------------------------------------


def fingerprint(*parts: Any) -> str:
    """Identifies the query a cursor belongs to, so it is not used with another."""
    text = json.dumps(parts, default=str, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def encode_cursor(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(payload), separators=(",", ":"), default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(text: str, kind: str, query: str) -> dict[str, Any]:
    """The cursor's content, refused unless it was made for this kind of list and query."""
    try:
        padded = text + "=" * (-len(text) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ApiError("the cursor is not one this API issued", code="api.bad_cursor") from exc
    if not isinstance(payload, dict) or payload.get("k") != kind:
        raise ApiError("the cursor is not one this API issued", code="api.bad_cursor")
    if payload.get("q") != query:
        raise ApiError(
            "the cursor belongs to another query; repeat the parameters of the first page",
            code="api.bad_cursor",
        )
    return payload


def page(table: pa.Table, offset: int, limit: int) -> tuple[pa.Table, int | None]:
    """Rows `offset` to `offset + limit` and the offset of the next page, if any."""
    end = offset + limit
    return table.slice(offset, limit), (end if end < table.num_rows else None)
