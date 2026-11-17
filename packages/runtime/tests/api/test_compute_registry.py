"""The registries of functions, models and questions: brought in line with the manifest,
added to and taken from while the twin runs, and read."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from compute_testkit import SEASON, Small, functions, models, plant_code
from dta_runtime.api import registry

FUNCTIONS = sorted(f"twin_function:{name}" for name in functions())
MODELS = sorted(
    f"twin_model:{spec}/{candidate}"
    for spec, found in models().items()
    for candidate in found["candidates"]
)
QUESTIONS = sorted(
    f"twin_question:{name}" for name in ("levels", "level_ahead", "demand_ahead", "level_whatif")
)


def registered(make: Callable[..., Small], history: int = 0) -> tuple[Small, str, dict[str, Any]]:
    api = make(history)
    code = api.upload(plant_code())
    return api, code, api.register(code)


def test_registering_writes_one_version_and_asking_again_writes_nothing(
    small: Callable[..., Small],
) -> None:
    api = small()
    code = api.upload(plant_code())
    before = api.twin.store.head()

    first = api.register(code)

    head = api.twin.store.head()
    assert head.seq == before.seq + 1 and head.t == before.t
    assert first["version"]["id"] == head.id
    assert sorted(first["functions"]["created"]) == FUNCTIONS
    assert first["functions"]["changed"] == [] and first["functions"]["disabled"] == []
    assert sorted(first["models"]["created"]) == MODELS
    assert sorted(first["questions"]["created"]) == QUESTIONS
    written = api.twin.store.versions(head.id)[0]
    assert written.cause == "registration" and written.actor == "registry"

    stored = api.store_rows("TwinFunction")
    assert sorted(stored) == FUNCTIONS
    report = stored["twin_function:tank_report"]
    assert report["kind"] == "registered" and report["artifact"] == f"twin_artifact:{code}"
    assert report["origin"] == "manifest" and report["status"] == "active"
    assert stored["twin_function:tank_levels"]["artifact"] is None
    assert stored["twin_function:tank_levels"]["latency_budget_ms"] == 5000
    kept = api.store_rows("TwinArtifact")[f"twin_artifact:{code}"]
    assert kept["sha256"] == code and kept["media_type"] == "application/zip"

    found = api.store_rows("TwinModel")
    assert {row["status"] for row in found.values()} == {"candidate"}
    assert found["twin_model:whatif/plant"]["code"] == f"twin_artifact:{code}"
    assert found["twin_model:whatif/plant"]["interventional"] is True
    assert found["twin_model:level/naive"]["code"] is None
    assert found["twin_model:level/naive"]["question"] == "level_ahead"

    asked = api.store_rows("TwinQuestion")
    assert {row["status"] for row in asked.values()} == {"provisional"}
    levels = asked["twin_question:levels"]
    assert levels["reason"] == "not measured yet" and levels["metric"] == "mae"
    assert levels["tolerance"] == 0.5 and levels["unit"] == "m"
    assert levels["function"] == "twin_function:tank_levels"
    assert asked["twin_question:level_whatif"]["function"] is None

    second = api.register(code)

    assert api.twin.store.head().id == head.id
    assert second["version"]["id"] == head.id
    for section, uids in (("functions", FUNCTIONS), ("models", MODELS), ("questions", QUESTIONS)):
        assert second[section]["created"] == [] and second[section]["changed"] == []
        assert sorted(second[section]["unchanged"]) == uids


def test_registering_needs_the_code_the_manifest_names(small: Callable[..., Small]) -> None:
    api = small()
    before = api.twin.store.head()

    api.refused(api.post("/lifecycle/register", json={}), 409, "register.code_missing")
    api.refused(api.post("/lifecycle/register"), 409, "register.code_missing")
    api.refused(api.post("/lifecycle/register", json={"code": "0" * 64}), 404, "artifact.not_found")
    api.refused(api.post("/lifecycle/register", json={"code": "nothing"}), 400, "api.bad_request")

    assert api.twin.store.head().id == before.id


def test_a_changed_candidate_is_a_candidate_again_and_what_is_gone_is_retired(
    small: Callable[..., Small],
) -> None:
    api, code, _ = registered(small)
    state = api.upload(b"a fitted state")
    services = api.services
    fitted = {
        "status": "active",
        "fitted": f"twin_artifact:{state}",
        "fitted_at_version": api.twin.store.head().id,
        "validity": "{}",
    }
    registry.commit(
        services,
        {
            "TwinArtifact": [
                registry.artifact_row(services, state, "application/octet-stream", "a state")
            ],
            "TwinModel": [
                {"uid": "twin_model:level/naive", **fitted},
                {"uid": "twin_model:level/theta", **fitted, "status": "candidate"},
            ],
        },
        note="fitted by the test",
    )
    kept = dict(functions())
    del kept["stuck"]
    candidates = {
        "naive": {"family": "seasonal_naive", "params": SEASON},
        "theta": {"family": "theta", "params": {"season_length": 8}},
    }
    api.revise(2, functions=kept, models=models(candidates))
    before = api.twin.store.head()

    answer = api.register(code)

    assert api.twin.store.head().seq == before.seq + 1
    assert answer["models"]["changed"] == ["twin_model:level/theta"]
    assert sorted(answer["models"]["retired"]) == [
        "twin_model:level/broken",
        "twin_model:level/lags",
    ]
    assert "twin_model:level/naive" in answer["models"]["unchanged"]
    assert answer["functions"]["disabled"] == ["twin_function:stuck"]
    found = api.store_rows("TwinModel")
    theta = found["twin_model:level/theta"]
    assert theta["status"] == "candidate" and theta["fitted"] is None
    assert theta["fitted_at_version"] is None and theta["validity"] is None
    assert theta["params"] == '{"season_length":8}'
    naive = found["twin_model:level/naive"]
    assert naive["status"] == "active" and naive["fitted"] == f"twin_artifact:{state}"
    assert found["twin_model:level/lags"]["status"] == "retired"
    assert api.store_rows("TwinFunction")["twin_function:stuck"]["status"] == "disabled"

    # The same code again changes nothing; other code is a change of the twin's own family.
    assert api.register(code)["version"]["id"] == api.twin.store.head().id
    other = api.upload(plant_code("another build"))
    again = api.register(other)
    assert again["models"]["changed"] == ["twin_model:whatif/plant"]
    assert "twin_function:tank_report" in again["functions"]["changed"]


def test_functions_are_registered_and_disabled_while_the_twin_runs(
    small: Callable[..., Small],
) -> None:
    api, _, _ = registered(small, history=2)
    body = {"id": "low_tanks", "entry": "below", "params": {"property": "Tank.level"}}

    made = api.post("/functions", json={**body, "latency_budget": "PT2S"})

    assert made.status_code == 201, made.text
    data = made.json()["data"]
    assert data["uid"] == "twin_function:low_tanks" and data["kind"] == "builtin"
    assert data["origin"] == "runtime" and data["status"] == "active"
    assert data["latency_budget_ms"] == 2000
    parameters = {found["name"]: found for found in data["parameters"]}
    assert parameters["property"]["fixed"] is True and parameters["threshold"]["fixed"] is False
    assert parameters["threshold"]["required"] is True
    assert made.json()["version"]["id"] == api.twin.store.head().id
    assert made.headers["x-dta-version-id"] == api.twin.store.head().id

    head = api.twin.store.head()
    api.refused(api.post("/functions", json=body), 409, "function.exists")
    refused = api.refused(
        api.post("/functions", json={"id": "nothing", "entry": "no_such_builtin"}),
        400,
        "ref.function_entry",
    )
    assert refused["errors"]
    api.refused(
        api.post(
            "/functions",
            json={"id": "lost", "entry": "latest", "params": {"property": "Tank.nothing"}},
        ),
        400,
        "ontology.unknown_property",
    )
    api.refused(
        api.post("/functions", json={"id": "own", "entry": "compute_plant:report"}),
        400,
        "ref.function_entry",
    )
    api.refused(
        api.post(
            "/functions",
            json={"id": "own", "entry": "compute_plant:report", "artifact": "0" * 64},
        ),
        404,
        "artifact.not_found",
    )
    api.refused(
        api.post("/functions", json={"id": "Bad Id", "entry": "latest"}), 400, "api.bad_request"
    )
    api.refused(api.delete("/functions/tank_levels"), 409, "function.from_manifest")
    api.refused(api.delete("/functions/nothing"), 404, "function.not_found")
    assert api.twin.store.head().id == head.id

    run = api.post("/functions/low_tanks/run", json={"threshold": 100})
    assert run.status_code == 200, run.text
    gone = api.delete("/functions/low_tanks")
    assert gone.status_code == 200 and gone.json()["data"]["status"] == "disabled"
    assert api.ok("/functions/low_tanks")["data"]["status"] == "disabled"
    api.refused(
        api.post("/functions/low_tanks/run", json={"threshold": 100}), 409, "function.disabled"
    )
    # Disabling again writes nothing.
    head = api.twin.store.head()
    assert api.delete("/functions/twin_function:low_tanks").status_code == 200
    assert api.twin.store.head().id == head.id


def test_functions_are_listed_a_page_at_a_time_as_of_a_version(
    small: Callable[..., Small],
) -> None:
    api, code, answer = registered(small)
    before = answer["version"]["seq"] - 1

    listed, cursor = [], None
    while True:
        page = api.ok("/functions", limit=3, **({"cursor": cursor} if cursor else {}))
        assert len(page["data"]) <= 3
        assert page["version"]["id"] == answer["version"]["id"]
        listed += page["data"]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert [found["uid"] for found in listed] == FUNCTIONS
    assert all("parameters" not in found for found in listed)
    assert api.ok("/functions", at=f"#{before}")["data"] == []

    one = api.ok("/functions/tank_report")["data"]
    assert one["kind"] == "registered" and one["artifact"] == code
    assert one["entry"] == "compute_plant:report" and one["parameters"] == []
    fixed = api.ok("/functions/junction_pressures")["data"]
    assert fixed["params"] == {"factor": 2.0}
    assert [found["name"] for found in fixed["parameters"]] == ["factor"]
    api.refused(api.get("/functions/nothing"), 404, "function.not_found")
    api.refused(api.get("/functions/tank_levels", at=f"#{before}"), 404, "function.not_found")
    api.refused(api.get("/functions", cursor="nothing"), 400, "api.bad_cursor")


def test_models_are_registered_and_retired_while_the_twin_runs(
    small: Callable[..., Small],
) -> None:
    api, code, _ = registered(small)
    body = {
        "spec": "level",
        "candidate": "last",
        "family": "persistence",
        "target": "Tank.level",
        "question": "level_ahead",
    }

    made = api.post("/models", json=body)

    assert made.status_code == 201, made.text
    data = made.json()["data"]
    assert data["uid"] == "twin_model:level/last" and data["status"] == "candidate"
    assert data["origin"] == "runtime" and data["question"] == "level_ahead"
    own = api.post(
        "/models",
        json={
            "spec": "whatif",
            "candidate": "again",
            "artifact": code,
            "entry": "compute_plant:balance",
            "target": "Tank.level",
            "features": ["Junction.demand"],
            "interventional": True,
        },
    )
    assert own.status_code == 201, own.text
    assert own.json()["data"]["family"] == "compute_plant:balance"
    assert own.json()["data"]["code"] == code

    head = api.twin.store.head()
    api.refused(api.post("/models", json=body), 409, "model.exists")
    other = {**body, "candidate": "other"}
    api.refused(api.post("/models", json={**other, "family": "nothing"}), 400, "ref.model_family")
    api.refused(
        api.post("/models", json={**other, "target": "Tank.nothing"}),
        400,
        "ontology.unknown_property",
    )
    api.refused(
        api.post("/models", json={**other, "target": "Nothing.level"}), 400, "ontology.unknown_type"
    )
    api.refused(
        api.post("/models", json={**other, "question": "nothing"}), 404, "question.not_found"
    )
    api.refused(api.post("/models", json={**other, "artifact": code}), 400, "api.bad_request")
    api.refused(api.delete("/models/twin_model:level/naive"), 409, "model.from_manifest")
    api.refused(api.delete("/models/twin_model:level/nothing"), 404, "model.not_found")
    assert api.twin.store.head().id == head.id

    gone = api.delete("/models/level/last")
    assert gone.status_code == 200 and gone.json()["data"]["status"] == "retired"
    assert api.ok("/models/twin_model:level/last")["data"]["status"] == "retired"


def test_models_and_questions_are_read_as_of_a_version(small: Callable[..., Small]) -> None:
    api, _, answer = registered(small)
    before = answer["version"]["seq"] - 1

    assert [found["uid"] for found in api.ok("/models")["data"]] == MODELS
    level = api.ok("/models", spec="level")["data"]
    assert [found["candidate"] for found in level] == ["broken", "lags", "naive", "theta"]
    assert api.ok("/models", status="active")["data"] == []
    asked = api.ok("/models", question="level_whatif", status="candidate")["data"]
    assert [found["uid"] for found in asked] == ["twin_model:whatif/plant"]
    first = api.ok("/models", limit=2)
    assert len(first["data"]) == 2 and first["next_cursor"]
    second = api.ok("/models", limit=2, cursor=first["next_cursor"])
    assert [found["uid"] for found in first["data"] + second["data"]] == MODELS[:4]
    assert api.ok("/models", at=f"#{before}")["data"] == []
    api.refused(api.get("/models", status="nothing"), 400, "api.bad_request")

    one = api.ok("/models/twin_model:level/lags")["data"]
    assert one["family"] == "ridge_lags" and one["params"] == {"lags": [1, 2], "date_features": []}
    assert one["fitted"] is None and one["state"] is None and one["target"] == "Tank.level"
    assert api.ok("/models/level/lags")["data"]["uid"] == "twin_model:level/lags"
    assert api.ok("/models/twin_model:level/lags/backtests")["data"] == []
    api.refused(api.get("/models/twin_model:level/nothing"), 404, "model.not_found")
    api.refused(api.get("/models/twin_model:level/nothing/backtests"), 404, "model.not_found")

    questions = api.ok("/questions")
    assert [found["uid"] for found in questions["data"]] == sorted(QUESTIONS)
    assert {found["status"] for found in questions["data"]} == {"provisional"}
    assert api.ok("/questions", status="validated")["data"] == []
    assert len(api.ok("/questions", status="provisional", limit=3)["data"]) == 3
    one = api.ok("/questions/level_ahead")["data"]
    assert one == {
        "id": "level_ahead",
        "uid": "twin_question:level_ahead",
        "status": "provisional",
        "reason": "not measured yet",
        "metric": "mae",
        "measured_error": None,
        "tolerance": 0.5,
        "unit": "m",
        "basis": None,
        "function": "level_forecast",
        "model": None,
        "backtest": None,
        "measured_at": None,
    }
    assert api.ok("/questions/twin_question:levels")["data"]["function"] == "tank_levels"
    api.refused(api.get("/questions/nothing"), 404, "question.not_found")
    assert api.ok("/questions", at=f"#{before}")["data"] == []

    assert api.ok("/scenarios")["data"] == []
    api.refused(api.get("/scenarios/0a1b2c3d4e5f"), 404, "scenario.not_found")
