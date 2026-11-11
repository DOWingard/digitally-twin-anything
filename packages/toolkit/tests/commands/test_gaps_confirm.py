"""`dta manifest gaps` and `dta manifest confirm`."""

INCOMPLETE = {
    "twin": {"id": "demo", "name": "Demo"},
    "contract": {
        "purpose": "Watch tank levels",
        "tier": "static",
        "questions": {"q1": {"text": "What is the level now?", "kind": "descriptive"}},
    },
    "ontology": {"path": "ontology/domain.yaml"},
    "sources": {"s1": {"connector": "file", "location": "data/levels.csv"}},
    "field_status": {"contract.purpose": "confirmed"},
}

USER_GAPS = [
    ("contract.questions.q1.kind", "proposed"),
    ("contract.questions.q1.priority", "unknown"),
    ("contract.questions.q1.staleness", "unknown"),
    ("contract.questions.q1.text", "proposed"),
    ("contract.questions.q1.tolerance", "unknown"),
    ("contract.tier", "proposed"),
    ("ontology.type_signature", "unknown"),
]
AGENT_GAPS = [
    ("contract.boundary", "unknown"),
    ("contract.questions.q1.function", "unknown"),
    ("contract.questions.q1.targets", "unknown"),
    ("functions", "unknown"),
    ("sources.s1.mappings", "unknown"),
    ("sources.s1.profile", "unknown"),
]


def _gaps(workspace):
    code, document = workspace.json("manifest", "gaps")
    assert code == 0
    by_owner = document["result"]["by_owner"]
    return {
        owner: [(entry["path"], entry["status"]) for entry in entries]
        for owner, entries in by_owner.items()
    }


def test_incomplete_manifest_lists_exactly_its_gaps(workspace):
    workspace.write_manifest(INCOMPLETE)
    assert _gaps(workspace) == {"user": USER_GAPS, "agent": AGENT_GAPS}
    code, document = workspace.json("manifest", "gaps")
    assert document["result"]["total"] == len(USER_GAPS) + len(AGENT_GAPS)
    entry = document["result"]["by_owner"]["user"][0]
    assert entry["description"] == "Descriptive, diagnostic, predictive or prescriptive"


def test_gaps_is_a_query_and_exits_0_whatever_it_finds(workspace):
    workspace.write_manifest(INCOMPLETE)
    result = workspace.run("manifest", "gaps")
    assert result.exit_code == 0
    assert "user (7)" in result.stdout and "agent (6)" in result.stdout


def test_confirm_closes_a_user_gap(workspace):
    workspace.write_manifest(INCOMPLETE)
    code, document = workspace.json("manifest", "confirm", "contract.tier")
    assert code == 0, document
    assert document["result"]["confirmed"] == ["contract.tier"]
    assert workspace.read_manifest()["field_status"]["contract.tier"] == "confirmed"
    assert ("contract.tier", "proposed") not in _gaps(workspace)["user"]

    code, document = workspace.json("manifest", "confirm", "contract.tier")
    assert code == 0
    assert document["result"] == {
        "confirmed": [],
        "unchanged": ["contract.tier"],
        "remaining_gaps": len(USER_GAPS) + len(AGENT_GAPS) - 1,
    }


def test_confirm_refuses_an_agent_field_an_empty_field_and_an_unknown_path(workspace):
    workspace.write_manifest(INCOMPLETE)
    before = workspace.root.joinpath("manifest.yaml").read_bytes()
    for path, reason in [
        ("twin.id", "confirm.not_user_field"),
        ("contract.questions.q1.staleness", "confirm.no_value"),
        ("contract.nowhere", "confirm.not_required"),
    ]:
        code, document = workspace.json("manifest", "confirm", path)
        assert code == 2, path
        assert document["error"]["code"] == "confirm.refused"
        assert document["error"]["detail"]["refused"][0]["code"] == reason

    # One refused path refuses the whole request.
    code, document = workspace.json("manifest", "confirm", "contract.tier", "twin.id")
    assert code == 2
    assert [entry["path"] for entry in document["error"]["detail"]["refused"]] == ["twin.id"]
    assert workspace.root.joinpath("manifest.yaml").read_bytes() == before


def test_confirm_all_user_confirms_every_user_field_with_a_value(workspace):
    workspace.write_manifest(INCOMPLETE)
    code, document = workspace.json("manifest", "confirm", "--all-user")
    assert code == 0
    assert document["result"]["confirmed"] == [
        "contract.questions.q1.kind",
        "contract.questions.q1.text",
        "contract.tier",
    ]
    assert document["result"]["unchanged"] == ["contract.purpose"]
    assert [path for path, status in _gaps(workspace)["user"] if status == "proposed"] == []


def test_confirm_needs_a_path_or_all_user(workspace):
    code, document = workspace.json("manifest", "confirm")
    assert code == 2
    assert document["error"]["code"] == "confirm.nothing"
