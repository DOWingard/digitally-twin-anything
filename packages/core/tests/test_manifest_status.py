import pytest
import yaml
from dta_core.errors import ManifestError
from dta_core.manifest import (
    Status,
    contract_hash,
    dump_manifest,
    gaps,
    manifest_hash,
    parse_manifest,
    required_fields,
    stale_status_entries,
    subtree_hash,
)


def _draft(**contract):
    base = {"purpose": "watch tank levels", "tier": "static"}
    base.update(contract)
    return {"twin": {"id": "demo", "name": "Demo"}, "contract": base}


def _paths(states):
    return {state.path for state in states}


def test_empty_manifest_reports_top_level_sections_as_unknown():
    assert _paths(gaps(parse_manifest({}))) == {
        "twin",
        "contract",
        "ontology",
        "sources",
        "functions",
    }


def test_user_fields_start_proposed_and_agent_fields_start_confirmed():
    manifest = parse_manifest(_draft())
    states = {state.path: state for state in required_fields(manifest)}
    assert states["contract.purpose"].status == Status.proposed
    assert states["twin.id"].status == Status.confirmed
    assert states["contract.questions"].status == Status.unknown


def test_explicit_status_closes_a_user_field():
    data = _draft()
    data["field_status"] = {"contract.purpose": "confirmed", "contract.tier": "confirmed"}
    open_paths = _paths(gaps(parse_manifest(data)))
    assert "contract.purpose" not in open_paths and "contract.tier" not in open_paths


def test_conditions_switch_requirements_on():
    static = _paths(required_fields(parse_manifest(_draft())))
    live = _paths(required_fields(parse_manifest(_draft(tier="live"))))
    assert {"sync", "fidelity", "contract.notification"} <= live
    assert not {"sync", "fidelity", "contract.notification"} & static

    question = {"text": "t", "kind": "descriptive", "priority": 1}
    descriptive = _paths(required_fields(parse_manifest(_draft(questions={"q": question}))))
    forecast = _paths(
        required_fields(parse_manifest(_draft(questions={"q": {**question, "kind": "predictive"}})))
    )
    assert "contract.questions.q.horizon" in forecast and "contract.questions.q.model" in forecast
    assert "contract.questions.q.horizon" not in descriptive
    assert "contract.objective" not in descriptive

    prescriptive = _paths(
        required_fields(
            parse_manifest(_draft(questions={"q": {**question, "kind": "prescriptive"}}))
        )
    )
    assert "contract.objective" in prescriptive
    controlled = _paths(
        required_fields(parse_manifest(_draft(controls={"c": {"autonomy": "advisory"}})))
    )
    assert "contract.objective" in controlled and "contract.controls.c.bounds" in controlled


def test_stale_status_entries_are_found():
    data = _draft()
    data["field_status"] = {"contract.purpose": "confirmed", "contract.gone": "confirmed"}
    assert stale_status_entries(parse_manifest(data)) == ["contract.gone"]


def test_unknown_keys_and_bad_values_are_schema_errors():
    with pytest.raises(ManifestError) as caught:
        parse_manifest({"contract": {"tier": "sometimes", "surprise": 1}})
    assert caught.value.code == "manifest.schema"
    assert {p["path"] for p in caught.value.detail["problems"]} == {
        "contract.tier",
        "contract.surprise",
    }
    with pytest.raises(ManifestError):
        parse_manifest(["not", "a", "mapping"])


def test_round_trip_and_hashes():
    data = _draft(tier="live", questions={"q": {"text": "t", "staleness": "PT15M"}})
    manifest = parse_manifest(data)
    again = parse_manifest(yaml.safe_load(dump_manifest(manifest)))
    assert manifest_hash(again) == manifest_hash(manifest)
    assert again.contract.questions["q"].staleness.total_seconds() == 900

    changed = parse_manifest({**data, "sync": {"interval": "PT5M"}})
    assert manifest_hash(changed) != manifest_hash(manifest)
    assert contract_hash(changed) == contract_hash(manifest)
    retoleranced = parse_manifest(_draft(tier="live", questions={"q": {"text": "other"}}))
    assert contract_hash(retoleranced) != contract_hash(manifest)

    assert subtree_hash(changed, ["contract", "sources.a"]) == subtree_hash(
        manifest, ["sources.a", "contract"]
    )
    assert subtree_hash(changed, ["sync"]) != subtree_hash(manifest, ["sync"])
