"""A dataset declared as read on by its time column: what the manifest's own checks see."""

from typing import Any

from dta_core.manifest import manifest_hash, parse_manifest
from dta_core.manifest.checks import run_checks


def _manifest(incremental: dict[str, str] | None, profiled: bool = True) -> Any:
    source: dict[str, Any] = {
        "connector": "http",
        "location": "http://plant:8080",
        "views": {"levels": 'SELECT * FROM "scada"."readings" WHERE quantity = \'level\''},
    }
    if incremental is not None:
        source["incremental"] = incremental
    if profiled:
        column = {"dtype": "double", "null_fraction": 0.0, "distinct_count": 10}
        source["profile"] = {
            "profiled_at": "2026-01-05T00:00:00Z",
            "datasets": {"readings": {"rows": 10, "columns": {"ts": column, "value": column}}},
        }
    return parse_manifest({"twin": {"id": "plant", "name": "Plant"}, "sources": {"scada": source}})


def _findings(manifest: Any) -> list[tuple[str, str]]:
    return [(f.path, f.message) for f in run_checks(manifest) if f.code == "ref.incremental"]


def test_a_dataset_with_its_time_column_passes():
    assert _findings(_manifest({"readings": "ts"})) == []
    # Before profiling there is nothing to compare the names with.
    assert _findings(_manifest({"elsewhere": "ts"}, profiled=False)) == []


def test_a_view_an_unknown_dataset_and_an_unknown_column_are_each_named():
    ((path, message),) = _findings(_manifest({"levels": "ts"}))
    assert path == "sources.scada.incremental.levels" and "is a view" in message
    ((path, message),) = _findings(_manifest({"history": "ts"}))
    assert path == "sources.scada.incremental.history" and "not in the profile" in message
    ((path, message),) = _findings(_manifest({"readings": "read_at"}))
    assert path == "sources.scada.incremental.readings" and "column 'read_at'" in message


def test_a_field_left_at_its_default_does_not_change_the_hash():
    """Writing out what the software would fill in anyway states nothing new."""
    assert manifest_hash(_manifest(None)) == manifest_hash(_manifest({}))
    assert manifest_hash(_manifest(None)) != manifest_hash(_manifest({"readings": "ts"}))
