"""The operator skill is a function of the manifest and gives nothing away."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
import yaml
from dta_core.manifest import Manifest, manifest_hash, parse_manifest
from dta_toolkit.skill import (
    MARKER_FILE,
    SKILL_FILE,
    SKILL_FORMAT,
    SkillError,
    render_skill,
    skill_name,
)

SECRET_LOCATION = "postgresql://${ERP_USER}:${ERP_PASSWORD}@erp.internal:5432/erp"


def manifest(**sections: Any) -> Manifest:
    document: dict[str, Any] = {
        "twin": {"id": "pump_station", "name": "Pump station"},
        "contract": {
            "purpose": "Keep the station running\nat the lowest energy cost",
            "tier": "static",
            "questions": {
                "q_late": {"text": "Asked second?", "kind": "diagnostic", "priority": 2},
                "q_first": {
                    "text": "What is the level | pressure now?",
                    "kind": "descriptive",
                    "priority": 1,
                    "tolerance": {"metric": "mae", "value": 0.05, "unit": "m"},
                    "staleness": "PT15M",
                    "targets": ["Tank.level", "Pump.pressure"],
                },
            },
        },
        "sources": {
            "erp": {
                "description": "Maintenance orders",
                "connector": "sql",
                "location": SECRET_LOCATION,
                "env": ["ERP_USER", "ERP_PASSWORD"],
            }
        },
    }
    document.update(sections)
    return parse_manifest(document, origin="test")


def render(document: Manifest):
    return render_skill(document, manifest_hash=manifest_hash(document))


def front_matter(text: str) -> dict[str, Any]:
    assert text.startswith("---\n")
    head, _, _ = text[4:].partition("\n---\n")
    return yaml.safe_load(head)


def test_skill_name_is_safe_as_a_directory_and_as_an_invocation():
    assert skill_name("pump_station") == "twin-pump-station"
    assert skill_name("Water.Static-2") == "twin-water-static-2"


def test_the_skill_has_its_files_and_a_listing_an_agent_can_use():
    skill = render(manifest())
    assert skill.name == "twin-pump-station"
    assert set(skill.files) == {
        SKILL_FILE,
        MARKER_FILE,
        "reference/api.md",
        "reference/recipes.md",
        "reference/errors.md",
    }
    front = front_matter(skill.files[SKILL_FILE])
    assert front["name"] == skill.name
    assert "Pump station" in front["description"]
    assert len(front["description"]) <= 1_000  # the listing is cut at 1,536 characters
    assert len(skill.files[SKILL_FILE].splitlines()) < 500
    for text in skill.files.values():
        assert "{{" not in text and "{%" not in text


def test_questions_come_in_priority_order_with_what_was_agreed():
    body = render(manifest()).files[SKILL_FILE]
    first, late = body.index("`q_first`"), body.index("`q_late`")
    assert first < late
    row = next(line for line in body.splitlines() if line.startswith("| `q_first`"))
    assert "mae ≤ 0.05 m" in row and "15 minutes" in row and "Tank.level, Pump.pressure" in row
    # A vertical bar in a question cannot end its table cell.
    assert "level \\| pressure" in row
    assert row.count(" | ") == 5


def test_each_question_says_how_its_answer_is_defined():
    document = manifest(
        functions={
            "f_low": {
                "description": "Objects whose property was below a threshold.",
                "kind": "builtin",
                "entry": "below",
                "params": {"threshold": 20, "property": "Tank.level"},
            }
        }
    )
    document.contract.questions["q_first"].function = "f_low"
    body = render(document).files[SKILL_FILE]
    assert (
        "- `q_first`: Objects whose property was below a threshold "
        "(property = Tank.level, threshold = 20)" in body
    )
    assert "- `q_late`:" not in body  # no function, nothing claimed about its method
    assert "How each answer is defined" not in render(manifest()).files[SKILL_FILE]


def test_the_marker_records_what_the_skill_was_rendered_from():
    document = manifest()
    skill = render(document)
    marker = json.loads(skill.files[MARKER_FILE])
    assert marker == {
        "format": SKILL_FORMAT,
        "skill": "twin-pump-station",
        "twin_id": "pump_station",
        "manifest_hash": manifest_hash(document),
    }
    assert manifest_hash(document)[:12] in skill.files[SKILL_FILE]


def test_the_same_manifest_gives_the_same_skill_and_a_changed_one_another():
    assert render(manifest()).files == render(manifest()).files
    changed = manifest(twin={"id": "pump_station", "name": "Pump station two"})
    assert render(changed).files[SKILL_FILE] != render(manifest()).files[SKILL_FILE]


def test_nothing_about_where_the_sources_are_reaches_the_skill():
    skill = render(manifest())
    everything = "\n".join(skill.files.values())
    for fragment in (SECRET_LOCATION, "erp.internal", "ERP_PASSWORD", "ERP_USER"):
        assert fragment not in everything


def test_variable_names_follow_the_manifest_and_tier_wording_follows_the_contract():
    document = manifest(interfaces={"base_url_env": "PUMP_URL", "token_env": "PUMP_TOKEN"})
    skill = render(document)
    for name in (SKILL_FILE, "reference/recipes.md", "reference/api.md"):
        assert "PUMP_URL" in skill.files[name] and "PUMP_TOKEN" in skill.files[name]
        assert "DTA_API_TOKEN" not in skill.files[name]
    assert "Tier: static" in skill.files[SKILL_FILE]
    live = manifest()
    live.contract.tier = type(live.contract.tier)("live")
    assert "Tier: live" in render(live).files[SKILL_FILE]


def live_manifest(**sections: Any) -> Manifest:
    document = manifest(**sections)
    document.contract.tier = type(document.contract.tier)("live")
    return document


def test_a_live_twin_is_told_how_to_judge_and_refresh_its_data():
    skill = render(live_manifest(sync={"interval": "PT15M"}))
    body = skill.files[SKILL_FILE]
    assert "## A live twin" in body and "every 15 minutes" in body
    for needed in (
        "`clock`",
        "`questions`",
        "`freshness_s`",
        "`fresh`",
        "/sync/sources",
        "POST /sync/runs",
    ):
        assert needed in body, needed
    assert "This API only reads" not in body
    recipes, api, errors = (
        skill.files[f"reference/{name}.md"] for name in ("recipes", "api", "errors")
    )
    assert "## 8. Work with a live twin" in recipes and "Idempotency-Key" in recipes
    assert '.json()["data"]' in recipes and 'sync["questions"]' in recipes
    assert "| `POST /sync/runs`" in api
    assert "`sync.busy`" in errors and "`twin.not_live`" in errors
    assert "make it read its sources now" in front_matter(body)["description"]
    # Without an interval in the manifest the skill still renders.
    assert "on a schedule" in render(live_manifest()).files[SKILL_FILE]


def test_a_static_twin_is_not_offered_what_only_a_live_twin_does():
    skill = render(manifest())
    body = skill.files[SKILL_FILE]
    assert "## A live twin" not in body and "POST /sync/runs" not in body
    assert "This API only reads" in body
    assert "POST /sync/runs" not in skill.files["reference/api.md"]
    assert "Work with a live twin" not in skill.files["reference/recipes.md"]
    # What its load did is still there to read.
    assert "/sync/runs" in body and "/sync/runs" in skill.files["reference/recipes.md"]


_SPAN = re.compile(r"`([^`\n]+)`")
_GROUPS = ("meta", "objects", "object", "graph", "history", "block", "lifecycle", "sync")


def _endpoint_path(span: str) -> str | None:
    """The API path a code span names, parameters levelled; None when it names none."""
    text = span.split(" ", 1)[1] if span.startswith(("GET ", "POST ")) else span
    path = re.split(r"[?\s]", text, maxsplit=1)[0]
    parts = path.strip("/").split("/")
    if not path.startswith("/") or parts[0] not in _GROUPS or "*" in path:
        return None
    return "/" + "/".join("{}" if part.startswith(("{", "<")) else part for part in parts)


@pytest.mark.parametrize("document", [manifest, live_manifest], ids=["static", "live"])
def test_every_endpoint_the_skill_mentions_is_in_its_own_reference(document: Any):
    skill = render(document())
    listed = {
        _endpoint_path(_SPAN.match(line[2:]).group(1))
        for line in skill.files["reference/api.md"].splitlines()
        if line.startswith(("| `GET /", "| `POST /"))
    }
    assert len(listed) > 20
    for name in (SKILL_FILE, "reference/recipes.md", "reference/errors.md"):
        mentioned = {_endpoint_path(span) for span in _SPAN.findall(skill.files[name])} - {None}
        assert mentioned and mentioned <= listed, (name, sorted(mentioned - listed))


def test_text_from_the_manifest_cannot_break_the_front_matter():
    document = manifest(twin={"id": "x", "name": 'Odd: "name"\n---\nname: other'})
    front = front_matter(render(document).files[SKILL_FILE])
    assert front["name"] == "twin-x"


@pytest.mark.parametrize("missing", ["twin", "contract"])
def test_a_manifest_without_identity_or_contract_gives_no_skill(missing: str):
    document = manifest(**{missing: None})
    with pytest.raises(SkillError) as caught:
        render(document)
    assert caught.value.code == "skill.incomplete"
