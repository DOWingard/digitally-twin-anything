"""A twin that computes, built and accepted on real containers: its worker image, its
code, its functions, models and questions, the commands that work on them, the
skill's recipes, what a second build leaves alone, and what each change or fault
makes fail.

One twin serves the whole module, its tests running in the order written: each
leaves the twin as the next expects it. Thresholds are fixed before measuring;
the times are printed.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import m5_recipes
import pytest
from dta_toolkit import codepack
from m3tk_kit import needs_stack
from m5_kit import CODE_DIR, REQUIREMENTS_FILE, M5Twin, worker_images

from conftest import Dta

pytestmark = needs_stack

FIRST_BUILD_LIMIT_S = 300.0
SECOND_BUILD_LIMIT_S = 60.0
ACCEPT_LIMIT_S = 300.0
STATIC_SKIPPED = {"sync", "ui"}


@pytest.fixture(scope="module")
def twin(tmp_path_factory: pytest.TempPathFactory) -> Iterator[M5Twin]:
    base = tmp_path_factory.mktemp("m5")
    with pytest.MonkeyPatch.context() as patch:
        (base / "home").mkdir()
        patch.setenv("HOME", str(base / "home"))
        made = M5Twin(Dta(base / "ws"), base)
        try:
            made.prepare()
            yield made
        finally:
            made.teardown()
            assert made.leaked() == []


def steps(result: dict[str, Any]) -> dict[str, tuple[str, str]]:
    return {entry["step"]: (entry["action"], entry["status"]) for entry in result["steps"]}


def detail(result: dict[str, Any], name: str) -> dict[str, Any]:
    return next(entry for entry in result["steps"] if entry["step"] == name)["detail"]


def check(result: dict[str, Any], ident: str) -> dict[str, Any]:
    return next(found for found in result["checks"] if found["id"] == ident)


def client(twin: M5Twin) -> httpx.Client:
    env = dict(
        line.split("=", 1)
        for line in twin.env_file.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.startswith("#")
    )
    return httpx.Client(
        base_url=env["DTA_BASE_URL"] + "/api/v1",
        headers={"Authorization": "Bearer " + env["DTA_API_TOKEN"]},
        timeout=120.0,
    )


def head(twin: M5Twin) -> dict[str, Any]:
    with client(twin) as api:
        health = api.get("/meta/health").json()
    return {"head": health["head"], "versions": health["versions"]}


def worker_image_in_use(twin: M5Twin) -> str:
    done = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={twin.project}",
            "--filter",
            "label=com.docker.compose.service=worker",
            "--format",
            "{{.Image}}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def built(twin: M5Twin, *args: str) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    result = twin.ok("build", *args)
    return result, time.perf_counter() - started


def durations(result: dict[str, Any]) -> str:
    return ", ".join(f"{e['step']} {e.get('duration_ms', 0) / 1000:.1f}" for e in result["steps"])


# -- the whole build -------------------------------------------------------------------


def test_a_whole_build_runs_every_step_and_the_twin_is_accepted(twin: M5Twin) -> None:
    result, took = built(twin)
    print(f"\nfirst build {took:.1f} s: {durations(result)}")
    assert steps(result) == {
        name: ("ran", "skipped" if name in STATIC_SKIPPED else "ok") for name in steps(result)
    }
    assert len(result["steps"]) == 14
    assert took <= FIRST_BUILD_LIMIT_S

    # The worker image: built for the requirements, and the one the worker runs.
    image = detail(result, "image")
    assert image["action"] == "built"
    assert worker_images(twin.id) == [image["tag"]]
    assert worker_image_in_use(twin) == image["tag"]
    print(f"worker image {image['tag']}: {image['size_bytes']} bytes, {image['build_ms']} ms")

    # The code: packed the same way again it gives the same hash, the one registered.
    code = detail(result, "code")
    packed = codepack.pack(twin.root / CODE_DIR)
    assert code["uploaded"] is True and code["sha256"] == packed.sha256
    assert codepack.pack(twin.root / CODE_DIR).sha256 == packed.sha256
    assert twin.build_record()["steps"]["code"]["detail"]["sha256"] == packed.sha256
    functions = detail(result, "functions")
    assert functions["code"] == packed.sha256
    assert functions["counts"]["functions"]["created"] == 4

    manifest = twin.dta.read_manifest()
    with client(twin) as api:
        registered = {f["id"] for f in api.get("/functions").json()["data"]}
        models = {m["uid"]: m for m in api.get("/models").json()["data"]}
        questions = {q["id"]: q for q in api.get("/questions").json()["data"]}
        # The registered function runs in the worker, from its own image's package.
        report = api.post("/functions/f_report/run", json={"uid": "sensor:n1"})
    assert registered == set(manifest["functions"])
    assert set(models) == {
        f"twin_model:{spec}/{candidate}"
        for spec, found in manifest["models"].items()
        for candidate in found["candidates"]
    }
    assert set(questions) == set(manifest["contract"]["questions"])
    assert report.status_code == 200 and report.json()["value"]["label"] == "Sensor:N1"

    # theta follows the rise that the seasonal baseline misses.
    assert models["twin_model:level/theta"]["status"] == "active"
    assert models["twin_model:level/naive"]["status"] == "rejected"
    assert models["twin_model:whatif/drift"]["status"] == "active"
    evaluated = detail(result, "models")
    assert sorted(evaluated["evaluated"]) == ["level", "whatif"] and evaluated["unaccepted"] == []
    assert {q["id"]: q["status"] for q in detail(result, "questions")["questions"]} == {
        "q_level": "validated",
        "q_ahead": "validated",
        "q_whatif": "validated",
    }

    started = time.perf_counter()
    accepted = twin.ok("accept")
    accept_s = time.perf_counter() - started
    print(f"accept {accept_s:.1f} s")
    assert accepted["passed"] is True and accepted["failed"] == []
    assert accept_s <= ACCEPT_LIMIT_S
    ids = [found["id"] for found in accepted["checks"]]
    assert ids[-11:] == [
        "questions.answered",
        "questions.error",
        "models.accepted",
        "compute.worker",
        "fidelity.state",
        "fidelity.schedule",
        "actions.registered",
        "notifications.configured",
        "sdk.usable",
        "mcp.reachable",
        "sql.readonly",
    ]
    answered = check(accepted, "questions.answered")["evidence"]["questions"]
    assert all(entry["time_ms"] <= entry["budget_ms"] for entry in answered.values())
    errors = check(accepted, "questions.error")["evidence"]["questions"]
    assert errors["q_ahead"]["measured_error"] <= errors["q_ahead"]["tolerance"]
    level = check(accepted, "models.accepted")["evidence"]["specs"]["level"]
    assert level["active"] == "twin_model:level/theta"
    assert level["backtest"]["error"] < level["backtest"]["baseline_error"]

    # The skill lists every endpoint the API serves for computing, and records the states.
    assert check(accepted, "skill.endpoints")["passed"]
    skill = next((twin.root / "skill").glob("*"))
    api_md = (skill / "reference" / "api.md").read_text(encoding="utf-8")
    for endpoint in (
        "POST /questions/{id}/answer",
        "POST /functions/{id}/run",
        "POST /models/evaluate",
        "POST /models/{uid}/predict",
        "POST /scenarios",
        "POST /derived",
        "GET /search",
    ):
        assert f"`{endpoint}" in api_md, endpoint
    body = (skill / "SKILL.md").read_text(encoding="utf-8")
    assert "| `q_ahead` | validated | backtest |" in body
    assert "read `GET /questions` for what each may claim now" in body


def test_a_second_build_changes_nothing(twin: M5Twin) -> None:
    before = head(twin)
    images = worker_images(twin.id)
    result, took = built(twin)
    print(f"\nsecond build {took:.1f} s: {durations(result)}")
    assert took <= SECOND_BUILD_LIMIT_S
    assert detail(result, "image")["action"] == "reused"
    assert detail(result, "code")["uploaded"] is False
    assert detail(result, "functions")["changed_anything"] is False
    assert detail(result, "models")["evaluated"] == []
    assert detail(result, "questions")["measured"] == []
    assert steps(result)["load"] == ("reused", "ok")
    assert head(twin) == before
    assert worker_images(twin.id) == images


# -- the commands -------------------------------------------------------------------------


def test_each_command_answers_with_json(twin: M5Twin) -> None:
    registered = twin.ok("register")
    assert registered["changed_anything"] is False and registered["code"]["uploaded"] is False
    assert registered["counts"]["questions"]["unchanged"] == 3

    listed = twin.ok("models", "list")["models"]
    assert {m["uid"]: m["status"] for m in listed}["twin_model:level/theta"] == "active"

    evaluated = twin.ok("models", "evaluate", "--spec", "whatif")
    assert [s["spec"] for s in evaluated["specs"]] == ["whatif"]
    assert evaluated["specs"][0]["selected"] == "twin_model:whatif/drift"
    assert evaluated["unaccepted"] == []
    started = twin.ok("models", "evaluate", "--no-wait")
    assert started["specs"][0]["job"]["kind"] == "evaluate"
    twin.failed("compute.bad_argument", "models", "evaluate", "--spec", "nowhere", exit_code=2)

    questions = twin.ok("questions", "list")["questions"]
    assert {q["id"] for q in questions} == {"q_level", "q_ahead", "q_whatif"}
    measured = twin.ok("questions", "measure")
    assert sorted(measured["measured"]) == ["q_ahead", "q_level", "q_whatif"]

    answer = twin.ok("questions", "answer", "q_level", "--arg", "property=Sensor.level")
    assert answer["arguments"] == {"property": "Sensor.level"}
    assert {row["uid"] for row in answer["value"]} == {"sensor:n1", "sensor:n2", "sensor:s1"}
    assert answer["provisional"] is False and answer["version"]["seq"] >= 1
    at_first = twin.ok(
        "questions", "answer", "q_level", "--arg", "property=Sensor.level", "--at", "#1"
    )
    assert at_first["version"]["seq"] == 1
    refused = twin.failed("function.param_missing", "questions", "answer", "q_level")
    assert refused["detail"]["status"] == 400

    text = twin.dta.run("questions", "answer", "q_level", "--arg", "property=Sensor.level")
    assert text.exit_code == 0 and "sensor:n1" in text.stdout and "uid" in text.stdout

    assert twin.ok("scenarios", "list")["scenarios"] == []


def test_the_skill_recipes_run_as_written(twin: M5Twin, monkeypatch: pytest.MonkeyPatch) -> None:
    env = dict(
        line.split("=", 1)
        for line in twin.env_file.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.startswith("#")
    )
    monkeypatch.setenv("DTA_BASE_URL", env["DTA_BASE_URL"])
    monkeypatch.setenv("DTA_API_TOKEN", env["DTA_API_TOKEN"])
    recipes = next((twin.root / "skill").glob("*/reference/recipes.md")).read_text("utf-8")
    before = head(twin)
    done = m5_recipes.run(recipes)
    assert {row["uid"] for row in done["answer"]["value"]} >= {"sensor:n1"}
    assert done["ran"]["value"]["label"] == "Sensor:N1" and done["ran"].get("wrote") is None
    assert done["backtest"]["accepted"] is True
    assert {row["unique_id"] for row in done["prediction"]["data"]} == {
        "sensor:n1",
        "sensor:n2",
        "sensor:s1",
    }
    assert [spec["spec"] for spec in done["result"]["specs"]] == ["level"]
    assert done["frame"].num_rows > 0 and done["meta"]
    # Half a metre an hour more inflow, for two hours, in every sensor the drift predicts.
    differences = {round(row["difference"], 6) for row in done["compared"]["data"]}
    assert {0.5, 1.0} <= differences <= {0.0, 0.5, 1.0}
    assert done["written"]["version"]["id"]
    assert done["found"]["total"] >= 1
    # Every scenario the recipe made is gone again.
    left = twin.ok("scenarios", "list")["scenarios"]
    assert all(s["status"] == "deleted" for s in left)
    assert head(twin)["head"]["seq"] > before["head"]["seq"]

    # What the reference says never changes the twin leaves its head where it was.
    with client(twin) as api:
        start = api.get("/meta/health").json()["head"]
        api.post("/questions/q_level/answer", json={"property": "Sensor.level"})
        api.post("/models/twin_model:level/theta/predict", json={"horizon": 2})
        api.post("/functions/f_report/run", json={"uid": "sensor:n1"})
        assert api.get("/meta/health").json()["head"] == start


# -- change and rebuild ---------------------------------------------------------------


def _rate(value: float):
    def change(data: dict[str, Any]) -> None:
        data["models"]["whatif"]["candidates"]["drift"]["params"]["rate"] = value

    return change


def test_a_changed_candidate_evaluates_only_its_specification(twin: M5Twin) -> None:
    twin.change_manifest(_rate(0.06))
    result, _ = built(twin)
    assert detail(result, "functions")["changes"]["models"]["changed"] == [
        "twin_model:whatif/drift"
    ]
    assert detail(result, "models")["evaluated"] == ["whatif"]
    assert detail(result, "questions")["measured"]


def test_a_changed_code_file_gives_a_new_hash_and_changed_registrations(twin: M5Twin) -> None:
    before = twin.build_record()["steps"]["code"]["detail"]["sha256"]
    path = twin.root / CODE_DIR / "plant.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n# a second revision\n", "utf-8")
    result, _ = built(twin)
    code = detail(result, "code")
    assert code["sha256"] != before and code["uploaded"] is True
    changes = detail(result, "functions")["changes"]
    assert changes["functions"]["changed"] == ["twin_function:f_report"]
    assert changes["models"]["changed"] == ["twin_model:whatif/drift"]
    assert detail(result, "models")["evaluated"] == ["whatif"]


def test_a_changed_requirements_file_gives_a_new_image(twin: M5Twin) -> None:
    old = worker_images(twin.id)
    path = twin.root / REQUIREMENTS_FILE
    path.write_text(path.read_text(encoding="utf-8") + "# pinned for the plant's labels\n", "utf-8")
    result, _ = built(twin)
    image = detail(result, "image")
    assert image["action"] == "built" and [image["tag"]] != old
    assert worker_image_in_use(twin) == image["tag"]
    with client(twin) as api:
        assert api.post("/functions/f_report/run", json={"uid": "sensor:n1"}).status_code == 200
    for tag in old:
        subprocess.run(["docker", "image", "rm", tag], capture_output=True, check=True)
    assert worker_images(twin.id) == [image["tag"]]


# -- what each fault makes fail -----------------------------------------------------------


def _tolerance(value: float):
    def change(data: dict[str, Any]) -> None:
        data["contract"]["questions"]["q_whatif"]["tolerance"]["value"] = value

    return change


def test_a_tolerance_below_the_measured_error_fails_only_questions_error(twin: M5Twin) -> None:
    # Above the noise floor the manifest's checks allow, below the drift model's error.
    twin.change_manifest(_tolerance(0.7))
    result, _ = built(twin)
    assert all(status != "failed" for _, status in steps(result).values())
    whatif = next(q for q in detail(result, "questions")["questions"] if q["id"] == "q_whatif")
    assert whatif["status"] == "failing" and whatif["measured_error"] > 0.7
    text = twin.dta.run("build")
    assert text.exit_code == 0 and "question q_whatif is failing" in text.stdout

    error = twin.failed("accept.failed", "accept")
    assert error["detail"]["failed"] == ["questions.error"]
    failing = check(error["detail"], "questions.error")
    assert "q_whatif: failing" in failing["evidence"]["failures"][0]
    assert "wider tolerance" in failing["remedy"]
    twin.change_manifest(_tolerance(1000.0))
    built(twin)


def test_a_spec_with_too_little_history_is_provisional_and_accepted(twin: M5Twin) -> None:
    def short(data: dict[str, Any]) -> None:
        _rate(0.05)(data)
        # The manifest's checks count the backtest's window, the API's plan the horizon too.
        data["contract"]["questions"]["q_whatif"]["horizon"] = "PT16H"

    twin.change_manifest(short)
    result, _ = built(twin)
    unaccepted = detail(result, "models")["unaccepted"]
    assert [entry["spec"] for entry in unaccepted] == ["whatif"]
    assert "forecast origins" in unaccepted[0]["reason"]
    accepted = twin.ok("accept")
    assert accepted["passed"] is True
    answered = check(accepted, "questions.answered")["evidence"]["questions"]["q_whatif"]
    assert answered["not_answerable"].startswith("provisional: the history has room for")
    spec = check(accepted, "models.accepted")["evidence"]["specs"]["whatif"]
    assert "forecast origins" in spec["reason"]

    def restore(data: dict[str, Any]) -> None:
        data["contract"]["questions"]["q_whatif"]["horizon"] = "PT2H"

    twin.change_manifest(restore)
    # The horizon its last evaluation took changed, so the specification is evaluated again.
    again, _ = built(twin)
    assert detail(again, "models")["evaluated"] == ["whatif"]
    assert detail(again, "models")["unaccepted"] == []
    # Building from the models step evaluates every specification.
    again, _ = built(twin, "--from", "models")
    assert sorted(detail(again, "models")["evaluated"]) == ["level", "whatif"]


def test_a_stopped_worker_fails_its_checks_and_the_questions_that_need_it(twin: M5Twin) -> None:
    subprocess.run(
        ["docker", "compose", "--project-name", twin.project, "stop", "worker"],
        capture_output=True,
        check=True,
    )
    error = twin.failed("accept.failed", "accept")
    assert error["detail"]["failed"] == ["questions.answered", "compute.worker"]
    failures = check(error["detail"], "questions.answered")["evidence"]["failures"]
    assert [line.split(":", 1)[0] for line in failures] == ["q_ahead", "q_whatif"]
    worker = check(error["detail"], "compute.worker")
    assert worker["evidence"]["worker"] == {"configured": True, "ok": False}
    assert "dta up worker" in worker["remedy"]
    twin.ok("up", "worker")
    assert twin.ok("accept")["passed"] is True


def test_code_without_a_module_the_manifest_names_fails_the_code_step(twin: M5Twin) -> None:
    path = twin.root / CODE_DIR / "plant.py"
    moved = path.with_name("plants.py")
    path.rename(moved)
    try:
        error = twin.failed("build.failed", "build")
        found = error["detail"]
        assert found["step"] == "code"
        assert found["not_attempted"] == [
            "functions",
            "models",
            "questions",
            "ui",
            "mcp",
            "skill",
            "sdk",
        ]
        assert found["evidence"]["code"] == codepack.CODE_NO_MODULE
        assert "has no module plant" in error["message"] and "add the module" in error["message"]
    finally:
        moved.rename(path)
    built(twin)


def test_a_requirement_without_a_package_fails_the_image_step(twin: M5Twin) -> None:
    path = twin.root / REQUIREMENTS_FILE
    kept = path.read_text(encoding="utf-8")
    path.write_text("dta-m5tk-no-such-package==1.0\n", encoding="utf-8")
    try:
        error = twin.failed("build.failed", "build")
        found = error["detail"]
        assert found["step"] == "image"
        assert found["not_attempted"][0] == "store"
        assert "No matching distribution found for dta-m5tk-no-such-package" in error["message"]
        assert "Correct runtime.worker_requirements" in error["message"]
        assert len(worker_images(twin.id)) == 1
    finally:
        path.write_text(kept, encoding="utf-8")
    result, _ = built(twin)
    assert detail(result, "image")["action"] == "reused"


def test_nothing_secret_was_printed(twin: M5Twin) -> None:
    assert twin.leaked() == []
    records = [json.dumps(twin.build_record())]
    reports = [p.read_text("utf-8") for p in (twin.root / "reports").glob("*") if p.is_file()]
    assert twin.leaked(records + reports) == []


def test_the_twin_root_holds_no_bytecode(twin: M5Twin) -> None:
    assert not list(Path(twin.root / CODE_DIR).rglob("*.pyc"))
