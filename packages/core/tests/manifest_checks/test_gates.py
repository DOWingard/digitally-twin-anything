from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from dta_core.manifest import access_hash, contract_hash, manifest_hash, parse_manifest, to_plain
from dta_core.manifest.checks import (
    GATES,
    STAGE_NAMES,
    GateInputs,
    GateOutcome,
    Verdict,
    computed_gate,
    current_stage,
    evaluate_gates,
    subject_of,
)

QUESTION = {"text": "Level now?", "kind": "descriptive", "priority": 1}
SOURCE = {"connector": "file", "location": "data/levels.csv"}


def _draft(**extra):
    data = {
        "twin": {"id": "demo", "name": "Demo"},
        "contract": {"purpose": "watch tanks", "tier": "static", "questions": {"q": QUESTION}},
        "sources": {"s": SOURCE},
    }
    data.update(extra)
    return parse_manifest(data)


def _outcomes(manifest, recorded=None, signatures=()):
    inputs = GateInputs(manifest, recorded=recorded, signatures=signatures)
    return {outcome.name: outcome for outcome in evaluate_gates(inputs)}


def test_registry_covers_every_stage():
    assert sorted(GATES) == list(range(9))
    assert {stage: gate.name for stage, gate in GATES.items()} == STAGE_NAMES
    assert {stage for stage, gate in GATES.items() if gate.recorded} == {2, 4, 6, 7, 8}


def test_intake_needs_identity_purpose_and_located_sources():
    outcome = _outcomes(parse_manifest({}))["intake"]
    assert outcome.verdict == Verdict.failed
    assert outcome.reasons == (
        "twin.id has no value",
        "contract.purpose has no value",
        "no source is declared",
    )
    partial = _draft(sources={"s": {"connector": "file"}})
    assert _outcomes(partial)["intake"].reasons == ("sources.s.location has no value",)
    assert _outcomes(_draft())["intake"].passed


def test_contract_gate_waits_for_user_confirmation_except_tolerance_and_staleness():
    question = {**QUESTION, "tolerance": {"metric": "mae", "value": 1}, "staleness": "PT1H"}
    manifest = _draft(
        contract={"purpose": "watch tanks", "tier": "static", "questions": {"q": question}}
    )
    outcome = _outcomes(manifest)["contract"]
    assert outcome.verdict == Verdict.failed
    assert outcome.reasons == (
        "contract.purpose is not confirmed",
        "contract.questions.q.kind is not confirmed",
        "contract.questions.q.priority is not confirmed",
        "contract.questions.q.text is not confirmed",
        "contract.tier is not confirmed",
    )
    confirmed = {
        path: "confirmed"
        for path in (
            "contract.purpose",
            "contract.tier",
            "contract.questions.q.kind",
            "contract.questions.q.priority",
            "contract.questions.q.text",
        )
    }
    manifest = _draft(
        contract={"purpose": "watch tanks", "tier": "static", "questions": {"q": question}},
        field_status=confirmed,
    )
    assert _outcomes(manifest)["contract"].passed


def test_recorded_gate_counts_only_while_its_subject_matches():
    manifest = _draft()
    assert _outcomes(manifest)["design"].verdict == Verdict.not_evaluated
    assert subject_of(4, manifest) == manifest_hash(manifest)

    current = {"design": SimpleNamespace(passed=True, subject_hash=manifest_hash(manifest))}
    assert _outcomes(manifest, current)["design"].verdict == Verdict.passed
    failed = {"design": SimpleNamespace(passed=False, subject_hash=manifest_hash(manifest))}
    assert _outcomes(manifest, failed)["design"].verdict == Verdict.failed
    changed = _draft(twin={"id": "demo", "name": "Renamed"})
    assert _outcomes(changed, current)["design"].verdict == Verdict.stale


def test_access_holds_until_the_way_a_source_is_reached_changes():
    manifest = _draft()
    probed = {"access": SimpleNamespace(passed=True, subject_hash=access_hash(manifest))}
    assert subject_of(2, manifest) == access_hash(manifest)
    assert _outcomes(manifest, probed)["access"].verdict == Verdict.passed

    # Anything but the sources' addresses and the networks leaves the probe valid.
    renamed = _draft(twin={"id": "demo", "name": "Renamed"})
    assert _outcomes(renamed, probed)["access"].verdict == Verdict.passed

    sources = to_plain(manifest)["sources"]
    first = sorted(sources)[0]
    moved = {**sources, first: {**sources[first], "location": "postgresql://${OTHER}/db"}}
    assert _outcomes(_draft(sources=moved), probed)["access"].verdict == Verdict.stale
    joined = _draft(runtime={"networks": ["plant-net"]})
    assert _outcomes(joined, probed)["access"].verdict == Verdict.stale


@pytest.mark.parametrize("stage", [6, 7, 8])
def test_build_acceptance_and_handover_hold_for_one_exact_manifest(stage):
    manifest = _draft()
    name = STAGE_NAMES[stage]
    recorded = {name: SimpleNamespace(passed=True, subject_hash=manifest_hash(manifest))}
    assert subject_of(stage, manifest) == manifest_hash(manifest)
    assert _outcomes(manifest, recorded)[name].verdict == Verdict.passed
    changed = _draft(twin={"id": "demo", "name": "Renamed"})
    assert _outcomes(changed, recorded)[name].verdict == Verdict.stale


def test_profiling_needs_a_profile_on_every_source():
    outcome = _outcomes(_draft())["profiling"]
    assert outcome.reasons == ("sources.s has no profile",)
    profiled = {**SOURCE, "profile": {"profiled_at": "2026-01-01T00:00:00Z", "datasets": {}}}
    assert _outcomes(_draft(sources={"s": profiled}))["profiling"].passed


def test_gaps_gate_needs_a_signature_on_the_current_contract():
    manifest = _draft()
    outcome = _outcomes(manifest)["gaps"]
    assert outcome.reasons[-1] == "the contract is not signed"
    assert outcome.reasons[0].endswith("gap(s) remain")

    def signature(digest):
        return SimpleNamespace(
            contract_hash=digest,
            mode="non_interactive",
            approved_by="Tester",
            signed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    old = _outcomes(manifest, signatures=[signature("0" * 64)])["gaps"]
    assert old.reasons[-1] == "the contract changed since it was signed"
    current = _outcomes(manifest, signatures=[signature(contract_hash(manifest))])["gaps"]
    assert "signature" in current.detail
    assert current.detail["signature"]["mode"] == "non_interactive"
    assert all("sign" not in reason for reason in current.reasons)


def test_current_stage_is_the_lowest_gate_not_passed():
    def outcome(stage, verdict):
        return GateOutcome(stage, STAGE_NAMES[stage], verdict)

    mixed = [
        outcome(0, Verdict.passed),
        outcome(1, Verdict.not_evaluated),
        outcome(2, Verdict.failed),
    ]
    assert current_stage(mixed) == 1
    assert current_stage([outcome(0, Verdict.passed), outcome(1, Verdict.passed)]) is None
    assert current_stage(_outcomes(parse_manifest({})).values()) == 0


def test_a_gate_can_be_replaced():
    original = GATES[2]
    try:

        @computed_gate(2)
        def access(inputs):
            return GateOutcome(2, "access", Verdict.passed)

        assert _outcomes(_draft())["access"].passed
    finally:
        GATES[2] = original
    assert _outcomes(_draft())["access"].verdict == Verdict.not_evaluated
