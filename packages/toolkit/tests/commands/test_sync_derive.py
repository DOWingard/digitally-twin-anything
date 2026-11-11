"""`dta sync derive`: the interval rule applied to the manifest of a workspace.

The complete sample states its sources' periods (1 min and 5 min, a rate limit
of 30 s) and its questions' stalenesses (10 min for the tank level, read from
the 1 min source). The rule therefore allows at most 10 - 1 = 9 min.
"""

from __future__ import annotations

import json
from typing import Any

from conftest import Dta


def _ready(workspace: Dta, complete: dict[str, Any], **sync: Any) -> Dta:
    """The complete sample in the workspace, its `sync` section replaced."""
    complete["sync"] = {"minimum_interval": "PT5M", "maximum_interval": "PT10M", **sync}
    complete["field_status"] = {
        path: status
        for path, status in complete["field_status"].items()
        if not path.startswith("sync.")
    }
    workspace.write_manifest(complete)
    return workspace


def test_derive_reports_the_interval_its_inputs_and_what_decided_it(
    workspace: Dta, complete: dict[str, Any]
) -> None:
    _ready(workspace, complete)
    before = workspace.read_manifest()

    code, document = workspace.json("sync", "derive")

    assert code == 0, document
    result = document["result"]
    assert (result["interval"], result["binding"], result["feasible"]) == (
        "PT9M",
        "freshness_bound",
        True,
    )
    assert result["inputs"] == {
        "source_period": "PT1M",
        "rate_period": "PT30S",
        "min_staleness": "PT10M",
        "freshness_bound": "PT9M",
        "change_time": None,
    }
    assert (result["manifest_interval"], result["agree"], result["written"]) == (None, False, False)
    # Without --write nothing is changed.
    assert workspace.read_manifest() == before


def test_write_records_the_interval_and_its_inputs_as_derived(
    workspace: Dta, complete: dict[str, Any]
) -> None:
    _ready(workspace, complete)

    code, document = workspace.json("sync", "derive", "--write")

    assert code == 0 and document["result"]["written"] is True
    written = workspace.read_manifest()
    assert written["sync"]["interval"] == "PT9M"
    assert written["sync"]["derivation"]["freshness_bound"] == "PT9M"
    assert written["sync"]["derivation"]["source_period"] == "PT1M"
    assert written["field_status"]["sync.interval"] == "derived"
    assert written["field_status"]["sync.derivation"] == "derived"
    # What was derived passes the manifest's own checks.
    code, validated = workspace.json("manifest", "validate")
    assert code == 0 and validated["result"]["errors"] == 0, validated
    # A second run finds the manifest in agreement and writes nothing.
    code, again = workspace.json("sync", "derive", "--write")
    assert code == 0
    assert (again["result"]["agree"], again["result"]["written"]) == (True, False)
    assert workspace.read_manifest() == written


def test_a_derived_interval_follows_the_rule_when_its_inputs_change(
    workspace: Dta, complete: dict[str, Any]
) -> None:
    _ready(workspace, complete)
    assert workspace.json("sync", "derive", "--write")[0] == 0
    changed = workspace.read_manifest()
    changed["contract"]["questions"]["q_level"]["staleness"] = "PT8M"
    workspace.write_manifest(changed)

    code, document = workspace.json("sync", "derive", "--write")

    assert code == 0 and document["result"]["written"] is True
    assert workspace.read_manifest()["sync"]["interval"] == "PT7M"


def test_a_confirmed_interval_is_reported_against_the_rule_and_never_replaced(
    workspace: Dta, complete: dict[str, Any]
) -> None:
    _ready(workspace, complete, interval="PT5M")
    before = workspace.read_manifest()

    code, document = workspace.json("sync", "derive")
    assert code == 0
    result = document["result"]
    assert (result["manifest_interval"], result["manifest_interval_confirmed"]) == ("PT5M", True)
    assert (result["interval"], result["agree"]) == ("PT9M", False)

    code, refused = workspace.json("sync", "derive", "--write")
    assert code == 1 and refused["error"]["code"] == "sync.interval_confirmed"
    assert workspace.read_manifest() == before


def test_a_contract_no_interval_can_meet_is_refused_with_the_reason(
    workspace: Dta, complete: dict[str, Any]
) -> None:
    # The pump source changes every 5 min; data no older than 4 min cannot be had from it.
    complete["contract"]["questions"]["q_level"]["staleness"] = "PT90S"
    complete["sources"]["scada"]["rate_limit_period"] = "PT2M"
    _ready(workspace, complete)
    before = workspace.read_manifest()

    code, document = workspace.json("sync", "derive", "--write")

    assert code == 1 and document["error"]["code"] == "sync.infeasible"
    detail = document["error"]["detail"]
    assert detail["feasible"] is False and detail["binding"] == "rate_period"
    assert any("question 'q_level'" in problem for problem in detail["problems"])
    assert workspace.read_manifest() == before


def test_nothing_known_means_nothing_derived(workspace: Dta) -> None:
    # The skeleton of a new workspace: no sources, no questions.
    code, document = workspace.json("sync", "derive")
    assert code == 1 and document["error"]["code"] == "sync.underivable"


def test_text_output_names_every_input(workspace: Dta, complete: dict[str, Any]) -> None:
    _ready(workspace, complete)
    result = workspace.run("sync", "derive")
    assert result.exit_code == 0, result.output
    for expected in ("source_period", "1 min", "freshness_bound", "9 min", "decided by"):
        assert expected in result.output
    # One line of JSON is not what text mode prints.
    try:
        json.loads(result.stdout)
    except ValueError:
        pass
    else:
        raise AssertionError("text mode printed JSON")
