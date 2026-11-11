"""`dta manifest sign` and the gate it feeds."""

import pytest
from dta_toolkit.commands import sign as sign_module

APPROVE = ("manifest", "sign", "--non-interactive", "--approved-by", "Test Runner")


@pytest.fixture
def signable(workspace, complete):
    workspace.write_manifest(complete)
    return workspace


def test_sign_refuses_while_gaps_remain(workspace, complete):
    del complete["field_status"]["contract.purpose"]
    workspace.write_manifest(complete)
    code, document = workspace.json(*APPROVE)
    assert code == 1
    assert document["error"]["code"] == "sign.gaps"
    assert [gap["path"] for gap in document["error"]["detail"]["gaps"]] == ["contract.purpose"]
    assert workspace.state()["signatures"] == []


def test_sign_refuses_with_error_findings(workspace, complete):
    complete["contract"]["objective"]["kpi"] = "f_missing"
    workspace.write_manifest(complete)
    code, document = workspace.json(*APPROVE)
    assert code == 1
    assert document["error"]["code"] == "sign.errors"
    assert workspace.state()["signatures"] == []


def test_sign_refuses_without_a_terminal(signable):
    code, document = signable.json("manifest", "sign")
    assert code == 2
    assert document["error"]["code"] == "sign.not_interactive"
    assert signable.state()["signatures"] == []


def test_non_interactive_sign_needs_a_name(signable):
    code, document = signable.json("manifest", "sign", "--non-interactive")
    assert code == 2
    assert document["error"]["code"] == "sign.approved_by_missing"


def test_non_interactive_sign_is_recorded_and_shown_as_such(signable):
    code, document = signable.json(*APPROVE)
    assert code == 0, document
    signature = document["result"]
    assert signature["mode"] == "non_interactive"
    assert signature["approved_by"] == "Test Runner"

    state = signable.state()
    assert state["signatures"][0]["contract_hash"] == signature["contract_hash"]
    assert state["gates"]["gaps"]["passed"] is True
    summary = signable.root.joinpath(".dta", "summary.md").read_text()
    assert signature["contract_hash"] in summary

    code, document = signable.json("status")
    status = document["result"]
    assert status["signature"]["mode"] == "non_interactive"
    assert status["signature"]["approved_by"] == "Test Runner"
    assert status["signature"]["current"] is True
    gaps_gate = next(gate for gate in status["gates"] if gate["name"] == "gaps")
    assert gaps_gate["result"] == "passed"
    assert gaps_gate["detail"]["signature"]["mode"] == "non_interactive"


def test_a_contract_change_fails_the_gate_and_an_outside_change_does_not(signable, complete):
    assert signable.json(*APPROVE)[0] == 0
    assert signable.gate("gaps")["result"] == "passed"

    outside = {**complete, "twin": {**complete["twin"], "name": "Renamed station"}}
    signable.write_manifest(outside)
    assert signable.gate("gaps")["result"] == "passed"

    changed = dict(complete)
    changed["contract"]["questions"]["q_level"]["text"] = "How full is each tank?"
    signable.write_manifest(changed)
    gate = signable.gate("gaps")
    assert gate["result"] == "failed"
    assert gate["reasons"] == ["the contract changed since it was signed"]
    code, document = signable.json("status")
    assert document["result"]["signature"]["current"] is False


def test_interactive_sign_requires_the_typed_twin_id(signable, monkeypatch):
    monkeypatch.setattr(sign_module, "_is_terminal", lambda: True)

    result = signable.run("manifest", "sign", input="pump\n")
    assert result.exit_code == 1
    assert "sign.declined" in result.stderr
    assert "# Contract summary: Pump Station" in result.stdout
    assert signable.state()["signatures"] == []

    result = signable.run("manifest", "sign", input="pump-station\n")
    assert result.exit_code == 0, result.output
    signature = signable.state()["signatures"][0]
    assert signature["mode"] == "interactive"
    assert signature["approved_by"] is None


def test_interactive_sign_with_json_keeps_stdout_a_single_document(signable, monkeypatch):
    monkeypatch.setattr(sign_module, "_is_terminal", lambda: True)
    code, document = signable.json("manifest", "sign", input="pump-station\n")
    assert code == 0
    assert document["result"]["mode"] == "interactive"


def test_interactive_sign_aborted_at_the_prompt_is_declined(signable, monkeypatch):
    monkeypatch.setattr(sign_module, "_is_terminal", lambda: True)
    code, document = signable.json("manifest", "sign", input="")
    assert code == 1
    assert document["error"]["code"] == "sign.declined"
