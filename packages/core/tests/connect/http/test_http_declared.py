"""A declared columns list is the list of columns kept, and the probe says what it left out.

A water utility's telemetry declared the columns of its readings without the
field the readings were later filtered on; nothing said that the field was gone.
"""

from __future__ import annotations

from dta_core.connect import probe
from dta_http_testkit import Api, Reply, connect, readings

DECLARED = {"sensor_id": "string", "timestamp": "timestamp[us, tz=UTC]", "value": "double"}


def _datasets() -> dict[str, dict[str, object]]:
    return {
        "declared": {"path": "/r", "records": "data", "columns": DECLARED},
        "inferred": {"path": "/r", "records": "data"},
    }


def test_fields_a_declaration_leaves_out_are_not_read_and_are_named_by_the_probe(
    api: Api,
) -> None:
    rows = readings(6)
    rows[4]["unit"] = "m"  # a field only some rows carry is named too
    api.route("/r", Reply({"data": rows}))
    with connect(api, _datasets()) as connector:
        read = list(connector.read("declared"))
        assert connector.dropped_columns("declared") == ("quality", "unit")
        report = probe(connector)
    assert {table.column_names == list(DECLARED) for table in read} == {True}
    by_name = {entry.dataset: entry for entry in report.datasets}
    assert [c.name for c in by_name["declared"].columns] == list(DECLARED)
    assert by_name["declared"].as_dict()["dropped_columns"] == ["quality", "unit"]
    # Without a declaration every field is kept, so nothing is left out.
    assert [c.name for c in by_name["inferred"].columns] == [
        "sensor_id",
        "timestamp",
        "value",
        "quality",
        "unit",
    ]
    assert by_name["inferred"].dropped_columns == ()


def test_a_declaration_naming_every_field_leaves_nothing_out(api: Api) -> None:
    api.route("/r", Reply({"data": readings(3)}))
    datasets = {
        "all": {"path": "/r", "records": "data", "columns": {**DECLARED, "quality": "string"}}
    }
    with connect(api, datasets) as connector:
        report = probe(connector)
    (entry,) = report.datasets
    assert entry.ok and entry.dropped_columns == ()
