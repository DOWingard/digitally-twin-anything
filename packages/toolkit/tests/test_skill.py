"""The operator skill is a function of the manifest and gives nothing away."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from dta_core.manifest import Manifest, manifest_hash, parse_manifest
from dta_core.ontology import load_ontology
from dta_core.schema import snake_case
from dta_runtime.mcp import api_document
from dta_runtime.mcp import operations as mcp_operations
from dta_runtime.mcp import tool_names as mcp_tool_names
from dta_toolkit import addons
from dta_toolkit.sdk import render_sdk
from dta_toolkit.skill import (
    MARKER_FILE,
    SKILL_FILE,
    SKILL_FORMAT,
    SkillError,
    render_skill,
    skill_name,
)
from dta_toolkit.stack import plan_stack

_ONTOLOGY = Path(__file__).parents[3] / "testbed" / "twin" / "ontology" / "water_utility.yaml"
SECRET_LOCATION = "postgresql://${ERP_USER}:${ERP_PASSWORD}@erp.internal:5432/erp"


def manifest(**sections: Any) -> Manifest:
    return parse_manifest(_document(**sections), origin="test")


def _document(**sections: Any) -> dict[str, Any]:
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
    return document


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
    assert "What else changes it: registrations" in body and "sync runs" not in body
    assert "a static twin's data was loaded once" in skill.files["reference/api.md"]
    assert "POST /sync/runs" not in skill.files["reference/api.md"]
    assert "Work with a live twin" not in skill.files["reference/recipes.md"]
    # What its load did is still there to read.
    assert "/sync/runs" in body and "/sync/runs" in skill.files["reference/recipes.md"]


_SPAN = re.compile(r"`([^`\n]+)`")
_GROUPS = (
    "meta",
    "objects",
    "object",
    "graph",
    "history",
    "block",
    "lifecycle",
    "sync",
    "functions",
    "models",
    "questions",
    "scenarios",
    "derived",
    "search",
    "fidelity",
    "actions",
    "control",
)


def _endpoint_path(span: str) -> str | None:
    """The API path a code span names, parameters levelled; None when it names none."""
    text = span.split(" ", 1)[1] if span.startswith(("GET ", "POST ")) else span
    path = re.split(r"[?\s]", text, maxsplit=1)[0]
    parts = path.strip("/").split("/")
    if not path.startswith("/") or parts[0] not in _GROUPS or "*" in path:
        return None
    return "/" + "/".join("{}" if part.startswith(("{", "<")) else part for part in parts)


def acting_manifest(*, acts: bool = True) -> Manifest:
    """A live twin with what it can check and, with `acts`, do: a forecast question, a
    control carried out by an executor, an advisory action, a loop, an objective, a
    webhook."""
    document = _document(
        sync={"interval": "PT15M"},
        fidelity={"interval": "PT1H", "window": "P7D"},
        models={"level": {"target": "Tank.level"}},
    )
    contract = document["contract"]
    contract["tier"] = "live"
    contract["questions"]["q_ahead"] = {
        "text": "Level in an hour?",
        "kind": "predictive",
        "priority": 3,
        "tolerance": {"metric": "mae", "value": 0.2, "unit": "m"},
        "staleness": "PT30M",
        "targets": ["Tank.level"],
        "horizon": "PT1H",
        "model": "level",
    }
    contract["notification"] = {"kind": "webhook", "webhook_env": "ALERT_URL"}
    if acts:
        contract["addons"] = {"executors": True}
        contract["controls"] = {
            "c_pump": {
                "description": "Run the pump",
                "target": "Pump.running",
                "bounds": {"allowed": [True, False]},
                "autonomy": "autonomous",
                "action": "set_pump",
            }
        }
        contract["objective"] = {
            "description": "Least energy cost",
            "direction": "minimize",
            "kpi": "f_cost",
        }
        document["actions"] = {
            "set_pump": {
                "description": "Switch the pump",
                "control": "c_pump",
                "object_type": "Pump",
                "binding": "executor",
                "executor": {"kind": "http", "url": "https://plant/pumps/{id}", "env": []},
                "parameters": {"value": {"type": "boolean"}},
                "settle": "PT30M",
            },
            "advise_valve": {"binding": "advisory", "description": "Open | close a valve"},
        }
        document["control_loops"] = {
            "l_pump": {"controls": ["c_pump"], "policy": "f_policy", "interval": "PT1H"}
        }
    return parse_manifest(document, origin="test")


def live_without_actions() -> Manifest:
    return acting_manifest(acts=False)


@pytest.mark.parametrize(
    "document",
    [manifest, live_manifest, live_without_actions, acting_manifest],
    ids=["static", "live", "live-without-actions", "live-with-everything"],
)
def test_every_endpoint_the_skill_mentions_is_in_its_own_reference(document: Any):
    skill = render(document())
    listed = {
        _endpoint_path(_SPAN.match(line[2:]).group(1))
        for line in skill.files["reference/api.md"].splitlines()
        if line.startswith(("| `GET /", "| `POST /"))
    }
    assert len(listed) > 20
    for name in sorted(set(skill.files) - {MARKER_FILE, "reference/api.md"}):
        mentioned = {_endpoint_path(span) for span in _SPAN.findall(skill.files[name])} - {None}
        assert mentioned and mentioned <= listed, (name, sorted(mentioned - listed))


# What only a live twin that checks itself, or acts, is told.
FIDELITY_TEXT = (
    "## Whether to trust an answer now",
    "## Incidents: what the twin does by itself",
    "reference/fidelity.md",
)
ACTING_TEXT = (
    "## Acting on the real system",
    "Never work around a blocked action",
    "Approval is the human's",
    "## Control loops",
    "Enabling a loop is the user's decision",
)


def test_a_static_twin_is_told_nothing_of_fidelity_actions_or_loops():
    skill = render(manifest(actions={"advise": {"binding": "advisory"}}))
    assert set(skill.files) == {
        SKILL_FILE,
        MARKER_FILE,
        "reference/api.md",
        "reference/recipes.md",
        "reference/errors.md",
    }
    body = skill.files[SKILL_FILE]
    for text in (*FIDELITY_TEXT, *ACTING_TEXT, "`healthy`", "`ok`: within tolerance"):
        assert text not in body, text
    everything = "\n".join(skill.files[name] for name in (SKILL_FILE, "reference/recipes.md"))
    for path in ("/fidelity", "/actions", "/control/loops"):
        assert path not in everything, path
    api = skill.files["reference/api.md"]
    assert "## Fidelity" not in api and "## Actions" not in api and "## Control loops" not in api
    assert "- actions:" not in api
    assert "Propose an action" not in skill.files["reference/recipes.md"]
    # What every twin's skill lists: the schedule, the notifications, the registration.
    for route in ("`GET /lifecycle/schedule`", "`GET /lifecycle/notifications`"):
        assert route in api


def test_a_live_twin_without_actions_is_told_of_its_fidelity_only():
    skill = render(live_without_actions())
    assert "reference/fidelity.md" in skill.files and "reference/remodel.md" in skill.files
    body = skill.files[SKILL_FILE]
    for text in FIDELITY_TEXT:
        assert text in body, text
    for text in ACTING_TEXT:
        assert text not in body, text
    assert (
        "`reconcile`" not in body and "Propose an action" not in skill.files["reference/recipes.md"]
    )
    assert "`failed`" in body and "report the answer as unreliable" in body
    fidelity = skill.files["reference/fidelity.md"]
    assert "Δ = 1 hour" in fidelity and "W = 7 days" in fidelity
    assert "| `q_ahead` | 1 hour | mae ≤ 0.2 m | `level` |" in fidelity
    for measure in ("Window error and breach", "## Warning", "## Staleness", "**Stuck.**"):
        assert measure in fidelity, measure
    assert "**Biased.**" in fidelity and "p₀ⁿ < 10⁻⁴" in fidelity
    # A series is biased only when every forecast question on its property agrees.
    assert "every other forecast question on the same property tested it" in fidelity
    assert "`signals.uncorroborated`" in fidelity


def test_a_live_twin_with_everything_is_told_the_interlock_and_who_decides():
    skill = render(acting_manifest())
    body = skill.files[SKILL_FILE]
    for text in (*FIDELITY_TEXT, *ACTING_TEXT, "`reconcile`", "reference/remodel.md"):
        assert text in body, text
    interlock = body[body.index("| Autonomy | `healthy`") :]
    assert interlock.startswith(
        "| Autonomy | `healthy` | `correcting`, only `breach` or `signal` incidents open "
        "| `correcting`, a `stale` or `reconcile` incident open | `unknown` | `failed` |"
    )
    assert (
        "| autonomous | executed at once | executed at once | `blocked` | `blocked` | `blocked` |"
        in interlock
    )
    assert (
        "| supervised | `proposed`; executed when a person approves it | same | same | same "
        "| an approval gives `blocked` |" in interlock
    )
    # The same rule in the API's reference, and the proposal recipe reads what it judges by.
    api = skill.files["reference/api.md"]
    assert "or `correcting` with no `stale` or `reconcile` incident unresolved" in api
    assert "unless the twin's fidelity is `healthy`" not in api
    assert 'standing["incidents"]' in skill.files["reference/recipes.md"]
    control = next(line for line in body.splitlines() if line.startswith("| `c_pump`"))
    assert "`Pump.running`" in control and "one of `true`, `false`" in control
    assert "autonomous" in control and "`set_pump`" in control
    advise = next(line for line in body.splitlines() if line.startswith("| `advise_valve`"))
    assert "Open \\| close a valve" in advise
    assert "| `l_pump` |" in body and "every loop starts disabled" in body.lower()
    assert "Least energy cost (minimize, measured by the function `f_cost`)" in body
    assert "by a webhook" in body
    assert "The twin executes none of these itself" not in body
    recipes = skill.files["reference/recipes.md"]
    assert "## 18. Propose an action" in recipes
    remodel = skill.files["reference/remodel.md"]
    for step in (
        "dta manifest validate",
        "dta build",
        "needs no new signature",
        "`requested_at` + 1 day",
        "Do not change the question, its tolerance or its horizon",
        "GET /fidelity/questions/{id}/errors",
        "/backtests",
    ):
        assert step in remodel, step
    description = front_matter(body)["description"]
    assert "fidelity state" in description and "propose actions" in description
    assert len(description) <= 1_000
    assert len(body.splitlines()) < 500


def test_the_recipe_files_are_there_exactly_when_they_apply():
    def files(document: Manifest) -> set[str]:
        return {name for name in render(document).files if name.startswith("reference/")} - {
            "reference/api.md",
            "reference/recipes.md",
            "reference/errors.md",
        }

    assert files(manifest()) == set()
    assert files(live_manifest()) == set()  # no fidelity interval and window
    assert files(acting_manifest()) == {"reference/fidelity.md", "reference/remodel.md"}
    # No forecast question with a model: nothing to re-model.
    document = acting_manifest()
    document.contract.questions["q_ahead"].model = None
    assert files(document) == {"reference/fidelity.md"}
    assert "**Biased.**" not in render(document).files["reference/fidelity.md"]
    # The managing agent may not be asked.
    document = acting_manifest()
    document.fidelity.correction.agent_remodel = False
    assert files(document) == {"reference/fidelity.md"}
    assert "remodel_requested" not in render(document).files[SKILL_FILE]


def test_without_the_executors_add_on_the_twin_executes_nothing_it_proposes():
    document = acting_manifest()
    document.contract.addons.executors = False
    body = render(document).files[SKILL_FILE]
    assert "The twin executes none of these itself" in body
    assert "`reconcile`" not in body


def test_the_skill_teaches_filters_on_links_and_not_a_search_by_hand():
    skill = render(manifest())
    recipes, api, errors = (
        skill.files[f"reference/{name}.md"] for name in ("recipes", "api", "errors")
    )
    assert "edge_where=<LinkType>.<property>:eq:<value>" in recipes
    assert "cannot leave out links" not in recipes
    assert "`GET /objects?uid_like=" in api and "x-dta-total" in api
    assert "`graph.bad_edge_filter`" in errors and "api.sort_too_large" not in errors


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


# -- the add-ons -------------------------------------------------------------------


def addon_manifest(**selected: bool) -> Manifest:
    data = _document()
    data["contract"]["addons"] = selected
    data["contract"]["questions"]["q_first"]["example"] = {"property": "Tank.level"}
    return parse_manifest(data, origin="test")


def _section(body: str, heading: str) -> str:
    start = body.index(heading)
    end = body.find("\n#", start + len(heading))
    return body[start : end if end != -1 else len(body)]


def _block(text: str, language: str) -> str:
    found = re.search(rf"```{language}\n(.*?)```", text, re.DOTALL)
    assert found, language
    return found.group(1)


@pytest.fixture(scope="module")
def sdk_code() -> dict[str, ast.ClassDef]:
    """The classes of the client package as it is generated for the skill's twin."""
    rendered = render_sdk(addon_manifest(sdk_python=True), load_ontology(_ONTOLOGY), api_document())
    tree = ast.parse(rendered.files[f"{rendered.package}/twin.py"].decode("utf-8"))
    return {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}


def _methods(cls: ast.ClassDef) -> dict[str, set[str]]:
    found = {}
    for node in cls.body:
        if isinstance(node, ast.FunctionDef):
            arguments = node.args
            found[node.name] = {a.arg for a in [*arguments.args, *arguments.kwonlyargs]}
    return found


def test_without_addons_the_skill_offers_none_and_keeps_agents_off_the_database():
    body = render(manifest()).files[SKILL_FILE]
    assert "## Other ways in" not in body
    assert "reach the twin's database or its sources directly" in body


@pytest.mark.parametrize(
    ("selected", "present"),
    [
        ({"sdk_python": True}, ["### Python client"]),
        ({"mcp": True}, ["### MCP tools"]),
        ({"readonly_sql": True}, ["### Read-only SQL"]),
        (
            {"sdk_python": True, "mcp": True, "readonly_sql": True},
            ["### Python client", "### MCP tools", "### Read-only SQL"],
        ),
    ],
    ids=["sdk", "mcp", "sql", "all"],
)
def test_each_selected_addon_has_its_section_and_no_other(selected, present):
    body = render(addon_manifest(**selected)).files[SKILL_FILE]
    assert "## Other ways in" in body
    for heading in ("### Python client", "### MCP tools", "### Read-only SQL"):
        assert (heading in body) == (heading in present), heading
    assert ("other than by the read-only SQL above" in body) == ("### Read-only SQL" in present)
    assert len(body.splitlines()) < 500


def test_what_the_skill_says_of_the_client_is_what_the_package_offers(sdk_code):
    body = render(addon_manifest(sdk_python=True)).files[SKILL_FILE]
    section = _section(body, "### Python client")
    assert "`twin_pump_station`" in section and f"`{'/'.join(addons.SDK_DIR)}/`" in section
    # Every call the section names, with the keywords it names, is in the generated code.
    classes = {name: _methods(cls) for name, cls in sdk_code.items()}
    owners = {"objects": "Twin", "head": "Twin", "ask": "Questions"}
    named = re.findall(r"`(?:twin\.(?:questions\.)?)?([a-z_]+)\(([^`]*)\)`", section)
    assert {name for name, _ in named} >= {"get", "list", "history", "links", "table", "ask"}
    for name, arguments in named:
        methods = classes[owners.get(name, "Objects")]
        assert name in methods, name
        for keyword in re.findall(r"([a-z_]+)=", arguments):
            assert keyword in methods[name], (name, keyword)
    reads = ("get", "list", "history", "links", "table")
    assert all({"at", "branch"} <= classes["Objects"][name] for name in reads)
    for error, fields in (("TwinConfigError", ()), ("TwinApiError", ("status", "code"))):
        assert f"`{error}`" in section and error in sdk_code
        source = ast.unparse(sdk_code[error])
        assert all(f"self.{field} =" in source for field in fields)
    example = _block(section, "python")
    compile(example, "skill-example", "exec")
    assert "twin.questions.ask(\"q_first\", property='Tank.level', at=version)" in example


def test_what_the_skill_says_of_the_mcp_adapter_is_what_it_serves():
    section = _section(render(addon_manifest(mcp=True)).files[SKILL_FILE], "### MCP tools")
    document = api_document()
    left_out = [op for op in mcp_operations(document) if op.excluded]
    assert len(left_out) == 2 and all("/artifacts" in op.path for op in left_out)
    assert "Two operations are not offered: uploading and fetching artifacts" in section
    assert set(mcp_tool_names(document)) == {
        op.operation_id for op in mcp_operations(document) if not op.excluded
    }
    registration = json.loads(_block(section, "json"))
    assert registration["mcpServers"].keys() == {"twin-pump-station"}
    (server,) = registration["mcpServers"].values()
    assert server["url"] == f"http://127.0.0.1:${{{addons.MCP_PORT_ENV}}}{addons.MCP_PATH}"
    assert server["headers"] == {"Authorization": "Bearer ${DTA_API_TOKEN}"}


def test_what_the_skill_says_of_read_only_sql_is_what_the_stack_grants():
    document = addon_manifest(readonly_sql=True)
    section = _section(render(document).files[SKILL_FILE], "### Read-only SQL")
    database = plan_stack(document).database
    assert f"`{database}`" in section and f"`{addons.READER_USER}`" in section
    for name in (addons.SQL_PORT_ENV, addons.SQL_READER_PASSWORD_ENV):
        assert f"`{name}`" in section
    granted = addons.grant_reader_sql(database, "a-good-password-0123")
    assert f"GRANT SELECT ON `{database}`.* TO '{addons.READER_USER}'@'%';" in granted
    # "Each type is a table named after it in snake_case".
    assert all(t.table == snake_case(t.name) for t in load_ontology(_ONTOLOGY).domain_types())
    statements = _block(section, "sql").strip().splitlines()
    assert any("AS OF" in s for s in statements) and any("dolt_diff_" in s for s in statements)
