"""The operator skill is a function of the manifest and gives nothing away."""

from __future__ import annotations

import json
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
